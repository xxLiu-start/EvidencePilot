from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.retrieval.embedding import BGEEmbeddingService, EmbeddingError
from app.retrieval.reranker import RerankerService, RerankerError
from app.retrieval.remote import validate_remote, authorized

def config(**overrides):
    return Settings(database_url="sqlite://", **{
        "environment": "test", "inference_mode": "remote",
        "inference_url": "http://inference:8000", "inference_token": "x" * 40,
        **overrides,
    })

def test_transport_policy():
    cfg = config()
    validate_remote(cfg)
    assert authorized(cfg, "Bearer " + "x" * 40)
    assert not authorized(cfg, "Bearer wrong")
    with pytest.raises(ValueError):
        validate_remote(config(environment="production"))
    with pytest.raises(ValueError):
        validate_remote(config(inference_url="https://user:pass@example.test"))
    with pytest.raises(ValueError):
        validate_remote(config(inference_token="short"))

def test_remote_embedding_batches_and_never_loads_local(monkeypatch):
    remote = Mock(side_effect=lambda cfg, path, payload, kind: [[2.] + [0.] * 1023 for _ in payload["texts"]])
    loader = Mock(side_effect=AssertionError("local loader called"))
    monkeypatch.setattr("app.retrieval.embedding.request_inference", remote)
    monkeypatch.setattr("app.retrieval.embedding._load_model", loader)
    svc = BGEEmbeddingService(config(embedding_batch_size=2))
    assert svc.embed_documents(["a", "b", "c"]) == [[1.] + [0.] * 1023] * 3
    assert remote.call_count == 2
    remote.side_effect = OSError("offline")
    with pytest.raises(EmbeddingError):
        svc.embed_query("query")
    loader.assert_not_called()

def test_remote_reranker_rejects_bad_output(monkeypatch):
    monkeypatch.setattr("app.retrieval.reranker.request_inference", lambda *args: [2.0])
    with pytest.raises(RerankerError):
        RerankerService(config()).score("q", [{"id": "a", "text": "a"}])

def test_inference_endpoint_auth_and_validation(monkeypatch):
    from app import inference_service as service
    monkeypatch.setattr(service, "local", config(inference_mode="local"))
    monkeypatch.setattr(service, "embedding", SimpleNamespace(embed_documents=lambda texts: [[1.] + [0.] * 1023 for _ in texts]))
    with TestClient(service.app) as client:
        assert client.post("/v1/embeddings", json={"texts": ["a"]}).status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 40}
        assert client.post("/v1/embeddings", headers=headers, json={"texts": ["a"] * 33}).status_code == 422
        response = client.post("/v1/embeddings", headers=headers, json={"texts": ["a"]})
        assert response.status_code == 200
        assert len(response.json()["values"][0]) == 1024
