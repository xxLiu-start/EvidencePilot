import json
import logging

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm_client import LLMConfigurationError, LLMError, ReasoningClient


class Output(BaseModel):
    queries: list[str]


def config(tmp_path, **kwargs):
    path = tmp_path / ".env"
    path.write_text('LLM_REMOTE_API_KEY="remote-file-secret"\n')
    return Settings(_env_file=None, database_url="sqlite://", credentials_env_file=str(path),
                    llm_local_base_url="http://127.0.0.1:8010/v1", llm_local_model="local-qwen",
                    llm_remote_base_url="https://remote.example/v1", llm_remote_model="remote-model", **kwargs)


def response(request, status=200, content='{"queries":["Qwen documentation"]}'):
    return httpx.Response(status, request=request, json={
        "id": "test", "object": "chat.completion", "created": 0, "model": "actual-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    } if status == 200 else {"error": {"message": "do not log response secrets"}})


def test_local_uses_placeholder_never_remote_key_and_trace_is_sanitized(tmp_path, caplog):
    captured = []

    def handle(request):
        captured.append(request)
        return response(request)

    caplog.set_level(logging.INFO)
    with_client = ReasoningClient(config(tmp_path), transport=httpx.MockTransport(handle))
    value, trace = with_client.complete_json([{"role": "user", "content": "private-prompt-sentinel"}], Output, operation="test")
    with_client.close()
    assert captured[0].headers["authorization"] == "Bearer local-no-auth"
    assert json.loads(captured[0].content)["model"] == "local-qwen"
    assert value.queries and trace.actual_model == "actual-model" and trace.mode == "local"
    assert "private-prompt-sentinel" not in caplog.text and "remote-file-secret" not in caplog.text
    assert "actual-model" in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_auth_and_argument_errors_not_retried(tmp_path, status):
    calls = []
    client = ReasoningClient(config(tmp_path), transport=httpx.MockTransport(
        lambda request: calls.append(request) or response(request, status)), sleep=lambda _: None)
    with pytest.raises(LLMError):
        client.complete_json([], Output, operation="test")
    client.close()
    assert len(calls) == 1


def test_transient_errors_retry_exact_bound_and_do_not_switch_backend(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return response(request, [429, 503, 200][len(calls) - 1])

    client = ReasoningClient(config(tmp_path), transport=httpx.MockTransport(handle), sleep=lambda _: None)
    _, trace = client.complete_json([], Output, operation="test")
    client.close()
    assert trace.attempts == 3 and trace.error_category is None
    assert {str(request.url.host) for request in calls} == {"127.0.0.1"}


def test_remote_https_and_its_own_key(tmp_path):
    cfg = config(tmp_path, llm_mode="remote")
    observed = []
    client = ReasoningClient(cfg, transport=httpx.MockTransport(
        lambda request: observed.append(request) or response(request)))
    client.complete_json([], Output, operation="remote-contract")
    client.close()
    assert observed[0].url.scheme == "https"
    assert observed[0].headers["authorization"] == "Bearer remote-file-secret"
    assert "chat_template_kwargs" not in json.loads(observed[0].content)
    cfg.llm_remote_base_url = "http://remote.example/v1"
    with pytest.raises(LLMConfigurationError):
        ReasoningClient(cfg)
    cfg.llm_remote_base_url = "https://remote.example/v1"
    (tmp_path / ".env").write_text("LLM_LOCAL_API_KEY=local-secret-only\n")
    with pytest.raises(LLMConfigurationError):
        ReasoningClient(cfg)


def test_invalid_json_is_not_retried(tmp_path):
    client = ReasoningClient(config(tmp_path), transport=httpx.MockTransport(
        lambda request: response(request, content="not JSON")))
    with pytest.raises(LLMError, match="invalid_json"):
        client.complete_json([], Output, operation="test")
    client.close()
