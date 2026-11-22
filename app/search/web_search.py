"""Tavily search with a private dotenv credential, bounded retries and snapshots."""

from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
from pathlib import Path
import time

import requests
from tavily import TavilyClient
from tavily.errors import BadRequestError, ForbiddenError, InvalidAPIKeyError, UsageLimitExceededError
from tavily.errors import TimeoutError as TavilyTimeoutError

from app.config import Settings, settings
from app.search.cache import CacheMiss, digest, write_json
from app.secrets import read_secret
from app.telemetry import audit_logger


logger = audit_logger(__name__)


class SearchError(RuntimeError):
    def __init__(self, category):
        super().__init__(f"Search failed: {category}")
        self.category = category


class WebSearchClient:
    def __init__(self, config: Settings = settings, *, sdk=None, sleep=time.sleep):
        self.config, self._sdk, self._sleep = config, sdk, sleep

    def search(self, query, max_results=5, *, cache_only=False, refresh=False):
        query = query.strip()
        if not query or not 1 <= max_results <= 10:
            raise ValueError("Require a non-empty query and 1–10 results")
        path = Path(self.config.web_cache_dir) / "searches" / (digest(json.dumps([query, max_results])) + ".json")
        start, attempts, category = time.perf_counter(), 0, None
        try:
            if path.is_file() and not refresh:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["query"] != query or cached["max_results"] != max_results:
                    raise CacheMiss("Search snapshot does not match request")
                return {**cached, "cache_hit": True,
                        "elapsed_ms": (time.perf_counter() - start) * 1000,
                        "original_elapsed_ms": cached["elapsed_ms"], "attempts": 0}
            if cache_only:
                raise CacheMiss("No fixed search snapshot")
            if self._sdk is None:
                key = read_secret("TAVILY_API_KEY", self.config.credentials_env_file)
                if not key:
                    raise SearchError("missing_credential")
                self._sdk = TavilyClient(api_key=key)
            for attempt in range(self.config.search_max_retries + 1):
                attempts = attempt + 1
                retryable = False
                try:
                    result = self._sdk.search(
                        query=query, max_results=max_results, search_depth="basic",
                        timeout=self.config.search_timeout_seconds,
                        include_answer=False, include_raw_content=False,
                    )
                    snapshot = {"query": query, "max_results": max_results,
                                "searched_at": datetime.now(timezone.utc).isoformat(),
                                "results": result.get("results", [])[:max_results],
                                "elapsed_ms": (time.perf_counter() - start) * 1000, "attempts": attempts}
                    category = None
                    write_json(path, snapshot)
                    return {**snapshot, "cache_hit": False}
                except UsageLimitExceededError:
                    category, retryable = "rate_limit", True
                except (InvalidAPIKeyError, ForbiddenError):
                    category = "authentication_or_quota"
                except BadRequestError:
                    category = "invalid_request"
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else 0
                    category, retryable = "server", 500 <= status < 600
                except (TimeoutError, TavilyTimeoutError, requests.Timeout, requests.ConnectionError):
                    category, retryable = "transport", True
                if not retryable or attempt == self.config.search_max_retries:
                    raise SearchError(category)
                self._sleep(min(.5 * 2 ** attempt, 2))
        except CacheMiss:
            category = "cache_miss"
            raise
        except SearchError as exc:
            category = exc.category
            raise
        except Exception:
            category = "invalid_response"
            raise SearchError(category) from None
        finally:
            logger.info("web_search %s", json.dumps({"query": query, "elapsed_ms": (time.perf_counter() - start) * 1000,
                                                     "attempts": attempts, "error_category": category}))


@lru_cache(maxsize=1)
def get_web_search_client():
    return WebSearchClient()


def search_web(query: str, max_results=5, **kwargs):
    return get_web_search_client().search(query, max_results=max_results, **kwargs)
