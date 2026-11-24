import httpx
import pytest
from fastapi import HTTPException
from app.config import Settings
from app.auth import introspection
from app.retrieval import remote

def test_live_revocation_and_outage(monkeypatch):
    cfg = Settings(database_url="sqlite://", environment="test",
        oidc_introspection_url="https://idp.test/introspect",
        oidc_introspection_client_id="api",
        oidc_introspection_client_secret="test-secret")
    replies = iter([{"active": True, "sub": "alice"}, {"active": False}, None])
    def handler(request):
        value = next(replies)
        if value is None:
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(200, json=value)
    real_client = httpx.Client
    monkeypatch.setattr(introspection.httpx, "Client", lambda **kw:
        real_client(transport=httpx.MockTransport(handler), **kw))
    claims = {"sub": "alice", "iss": "https://idp.test"}
    introspection.assert_active(cfg, "synthetic-token", claims)
    for status in (401, 503):
        with pytest.raises(HTTPException) as exc:
            introspection.assert_active(cfg, "synthetic-token", claims)
        assert exc.value.status_code == status

def test_circuit_and_recovery(monkeypatch):
    cfg = Settings(database_url="sqlite://", environment="test",
        inference_url="http://inference.test:8000",
        inference_token="a" * 32, inference_max_retries=1)
    clock = [100.0]
    calls = []
    def fail(*args):
        calls.append(1)
        raise httpx.ConnectError("down")
    monkeypatch.setattr(remote, "_circuits", {})
    monkeypatch.setattr(remote, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote, "sleep", lambda _: None)
    monkeypatch.setattr(remote, "_request_once", fail)
    for _ in range(3):
        with pytest.raises(httpx.ConnectError):
            remote.request_inference(cfg, "/v1/embeddings", {}, "embedding")
    assert len(calls) == 6
    with pytest.raises(remote.CircuitOpen):
        remote.request_inference(cfg, "/v1/embeddings", {}, "embedding")
    assert len(calls) == 6
    clock[0] += 16
    monkeypatch.setattr(remote, "_request_once", lambda *args: [1])
    assert remote.request_inference(cfg, "/v1/embeddings", {}, "embedding") == [1]
    assert remote.request_inference(cfg, "/v1/embeddings", {}, "embedding") == [1]

def test_no_retry_on_unauthorized(monkeypatch):
    cfg = Settings(database_url="sqlite://", environment="test",
        inference_url="http://inference.test:8000",
        inference_token="a" * 32, inference_max_retries=2)
    calls = []
    def fail(*args):
        calls.append(1)
        req = httpx.Request("POST", cfg.inference_url)
        raise httpx.HTTPStatusError("unauthorized", request=req,
            response=httpx.Response(401, request=req))
    monkeypatch.setattr(remote, "_circuits", {})
    monkeypatch.setattr(remote, "_request_once", fail)
    with pytest.raises(httpx.HTTPStatusError):
        remote.request_inference(cfg, "/v1/embeddings", {}, "embedding")
    assert len(calls) == 1
