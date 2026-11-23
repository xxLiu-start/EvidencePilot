import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel
import yaml

from app.config import Settings
from app.llm_client import LLMConfigurationError, LLMError, ReasoningClient
from app.search.web_search import WebSearchClient, SearchError
from tavily.errors import TimeoutError as TavilyTimeoutError


class Answer(BaseModel):
    ok: bool


def configuration(tmp_path, mode):
    secret = tmp_path/"credentials.env"
    secret.write_text("LLM_LOCAL_API_KEY=local-fault-secret\nLLM_REMOTE_API_KEY=remote-fault-secret\n")
    return Settings(_env_file=None, database_url="sqlite://", credentials_env_file=str(secret), llm_mode=mode,
        llm_local_base_url="http://local.test/v1", llm_local_model="local-model",
        llm_remote_base_url="https://remote.test/v1", llm_remote_model="remote-model")


@pytest.mark.parametrize("mode,kind,expected,count", [("local", "unreachable", "transport", 3), ("remote", "bad_key", "authentication", 1)])
def test_selected_backend_failure_never_falls_back_or_logs_secrets(tmp_path, caplog, mode, kind, expected, count):
    caplog.set_level(logging.INFO)
    requests, traces = [], []
    def handle(request):
        requests.append(request)
        if kind == "unreachable": raise httpx.ConnectError("private-upstream-sentinel", request=request)
        return httpx.Response(401, request=request, json={"error": {"message": "remote-fault-secret private-upstream-sentinel"}})
    cfg = configuration(tmp_path, mode)
    client = ReasoningClient(cfg, transport=httpx.MockTransport(handle), sleep=lambda _: None, trace_sink=traces.append)
    try:
        with pytest.raises(LLMError) as failed:
            client.complete_json([{"role": "user", "content": "private-prompt-sentinel"}], Answer, operation="fault_isolation")
        assert failed.value.category == expected
    finally: client.close()
    assert len(requests) == count and {r.url.host for r in requests} == {f"{mode}.test"}
    assert traces[0]["mode"] == mode and traces[0]["error_category"] == expected
    assert traces[0]["requested_model"] == f"{mode}-model"
    assert all(value not in caplog.text for value in ["local-fault-secret", "remote-fault-secret", "private-prompt-sentinel", "private-upstream-sentinel"])


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_missing_selected_configuration_fails_before_any_http(tmp_path, mode):
    cfg = configuration(tmp_path, mode)
    setattr(cfg, f"llm_{mode}_base_url", "")
    requests = []
    with pytest.raises(LLMConfigurationError):
        ReasoningClient(cfg, transport=httpx.MockTransport(lambda request: requests.append(request)))
    assert not requests


def test_compose_frontend_has_only_api_address_and_no_credentials_mount():
    config = yaml.safe_load(Path("docker-compose.yml").read_text())
    services = config["services"]
    assert {"api", "postgres", "streamlit"} <= services.keys()
    assert set(services["streamlit"]["environment"]) == {"API_BASE_URL", "STREAMLIT_OIDC_ENABLED"}
    assert not any("SECRET" in key or "TOKEN" in key or "PASSWORD" in key
                   for key in services["streamlit"]["environment"])
    assert not services["streamlit"].get("env_file") and not services["streamlit"].get("volumes")
    assert services["api"]["environment"]["LLM_LOCAL_BASE_URL"].find("host.docker.internal") >= 0
    assert services["postgres"]["volumes"]


def test_tavily_timeout_retries_are_bounded_and_never_save_failed_cache(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path))
    calls = []
    def search(**kwargs):
        calls.append(kwargs)
        raise TavilyTimeoutError("private-tavily-upstream-sentinel")
    client = WebSearchClient(cfg, sdk=SimpleNamespace(search=search), sleep=lambda _: None)
    with pytest.raises(SearchError) as failure:
        client.search("non-sensitive deployment documentation")
    assert failure.value.category == "transport" and len(calls) == cfg.search_max_retries+1
    assert all(item["timeout"] == cfg.search_timeout_seconds for item in calls)
    assert not list(tmp_path.rglob("*.json"))
    assert "private-tavily-upstream-sentinel" not in caplog.text
