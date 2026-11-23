"""One selected OpenAI-compatible client, bounded retries, sanitized traces."""

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import logging
import time
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings, settings
from app.secrets import read_secret
from app.telemetry import audit_logger
from app.observability.metrics import record_llm_trace


logger = audit_logger(__name__)


class LLMConfigurationError(ValueError):
    pass


class LLMError(RuntimeError):
    def __init__(self, category, validation_errors=None):
        super().__init__(f"Inference failed: {category}")
        self.category = category
        self.validation_errors = validation_errors or []


@dataclass(frozen=True)
class InferenceTrace:
    operation: str
    mode: str
    requested_model: str
    actual_model: str | None
    elapsed_ms: float
    attempts: int
    error_category: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    system_fingerprint: str | None = None


def selected_branch(config: Settings):
    local = config.llm_mode == "local"
    base = config.llm_local_base_url if local else config.llm_remote_base_url
    model = config.llm_local_model if local else config.llm_remote_model
    # Legacy generic keys are never used as credentials for another branch.
    if local:
        base = base or config.llm_base_url
        model = model or config.llm_model
    parsed = urlsplit(base)
    allowed = {"http", "https"} if local else {"https"}
    if (parsed.scheme not in allowed or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not model.strip()):
        raise LLMConfigurationError("Selected LLM branch requires a valid URL and model ID")
    credential_name = "LLM_LOCAL_API_KEY" if local else "LLM_REMOTE_API_KEY"
    key = read_secret(credential_name, config.credentials_env_file)
    if local:
        key = key or "local-no-auth"
    elif (len(key) < 8 or key.casefold() in {"local-no-auth", "not-needed", "changeme", "your-api-key", "sk-placeholder"}
          or key.startswith("<")):
        raise LLMConfigurationError("Remote HTTPS mode requires its own valid remote credential")
    return base.rstrip("/"), model.strip(), key


class ReasoningClient:
    def __init__(self, config: Settings = settings, *, transport=None, sleep=time.sleep, trace_sink=None):
        self.config = config
        self.trace_sink = trace_sink
        base, self.model, key = selected_branch(config)
        logging.getLogger("openai").setLevel(logging.WARNING)
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=config.llm_timeout_seconds, trust_env=False, transport=transport
        )
        self._client = OpenAI(base_url=base, api_key=key, max_retries=0, http_client=self._http)

    def close(self):
        self._client.close()

    def complete_json(self, messages, output_type: type[BaseModel], *, operation, max_tokens=1600, timeout_seconds=None):
        start = time.perf_counter()
        actual_model, error, attempts = None, None, 0
        usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "system_fingerprint": None}
        deadline = time.monotonic() + (timeout_seconds or self.config.llm_timeout_seconds * (self.config.llm_max_retries + 1))
        try:
            for attempt in range(self.config.llm_max_retries + 1):
                attempts = attempt + 1
                retryable = False
                try:
                    options = {}
                    if self.config.llm_mode == "local" and self.config.llm_disable_thinking:
                        options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LLMError("transport")
                    response = self._client.with_options(timeout=min(self.config.llm_timeout_seconds, remaining)).chat.completions.create(
                        model=self.model, messages=messages, temperature=0, max_tokens=max_tokens,
                        response_format={"type": "json_object"}, **options,
                    )
                    actual_model = response.model
                    usage["system_fingerprint"] = getattr(response, "system_fingerprint", None)
                    if response.usage is not None:
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            count = getattr(response.usage, key, None)
                            if count is not None:
                                usage[key] = (usage[key] or 0) + count
                    if not actual_model or not response.choices or response.choices[0].finish_reason != "stop":
                        raise LLMError("incomplete_response")
                    value = output_type.model_validate_json(response.choices[0].message.content or "")
                    error = None
                    trace = InferenceTrace(operation, self.config.llm_mode, self.model, actual_model,
                                           (time.perf_counter() - start) * 1000, attempts, None, **usage)
                    return value, trace
                except APIStatusError as exc:
                    error = "authentication" if exc.status_code in {401, 403} else (
                        "rate_limit" if exc.status_code == 429 else "server" if exc.status_code >= 500 else "invalid_request")
                    retryable = exc.status_code == 429 or 500 <= exc.status_code < 600
                except (APITimeoutError, APIConnectionError):
                    error, retryable = "transport", True
                except ValidationError as exc:
                    errors = [{"loc": list(item["loc"]), "type": item["type"]}
                              for item in exc.errors(include_input=False, include_url=False, include_context=False)]
                    raise LLMError("invalid_json", errors) from None
                except json.JSONDecodeError:
                    raise LLMError("invalid_json") from None
                if not retryable or attempt == self.config.llm_max_retries:
                    raise LLMError(error)
                self._sleep(min(0.5 * 2 ** attempt, 2))
        except LLMError as exc:
            error = exc.category
            raise
        except Exception:
            error = "unexpected_response"
            raise LLMError(error) from None
        finally:
            trace = InferenceTrace(operation, self.config.llm_mode, self.model, actual_model,
                                   (time.perf_counter() - start) * 1000, attempts, error, **usage)
            if self.trace_sink is not None:
                self.trace_sink(asdict(trace))
            # Never log messages, response text, headers, URLs or exception bodies.
            logger.info("llm_trace %s", json.dumps(asdict(trace)))
            record_llm_trace(asdict(trace))


@lru_cache(maxsize=1)
def get_llm_client():
    return ReasoningClient()
