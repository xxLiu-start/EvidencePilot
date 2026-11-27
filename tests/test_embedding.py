from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.retrieval.embedding import (
    BGEEmbeddingService, BGE_M3_DIMENSIONS, EmbeddingError, _load_model, validate_embeddings,
)


def vector(value=1.0):
    return [value] + [0.0] * (BGE_M3_DIMENSIONS - 1)


def test_lazy_model_reused_batched_and_queries_share_encoder(monkeypatch):
    loads, batches = [], []

    def encode(texts, **kwargs):
        batches.append((texts, kwargs))
        return {"dense_vecs": [vector(2.0) for _ in texts]}

    def load(config):
        loads.append(config)
        return SimpleNamespace(encode=encode)

    monkeypatch.setattr("app.retrieval.embedding._load_model", load)
    service = BGEEmbeddingService(Settings(database_url="sqlite://", embedding_batch_size=2))
    assert service.embed_documents([]) == []
    assert not loads
    assert service.embed_documents(["中文", "English", "third"]) == [vector()] * 3
    assert service.embed_query("query") == vector()
    assert len(loads) == 1
    assert [texts for texts, _ in batches] == [["中文", "English"], ["third"], ["query"]]
    assert all(kwargs == dict(
        batch_size=2, max_length=8192, return_dense=True,
        return_sparse=False, return_colbert_vecs=False,
    ) for _, kwargs in batches)


@pytest.mark.parametrize("vectors,count", [
    ([[1.0, 2.0]], 1),
    ([vector()], 2),
    ([vector(0)], 1),
    ([vector(float("nan"))], 1),
    ([vector(float("inf"))], 1),
    ([vector("invalid")], 1),
])
def test_invalid_vectors_rejected(vectors, count):
    with pytest.raises(EmbeddingError):
        validate_embeddings(vectors, count)


def test_vectors_are_normalized_and_json_serializable():
    values = np.ones((2, BGE_M3_DIMENSIONS), dtype=np.float16)
    output = validate_embeddings(values, 2)
    assert isinstance(output, list) and isinstance(output[0][0], float)
    np.testing.assert_allclose(np.linalg.norm(output, axis=1), [1, 1], rtol=1e-6)


def test_model_load_error_is_retryable(monkeypatch):
    service = BGEEmbeddingService(Settings(database_url="sqlite://"))

    def fail(config):
        raise OSError("model not downloaded")

    monkeypatch.setattr("app.retrieval.embedding._load_model", fail)
    with pytest.raises(EmbeddingError):
        service.embed_query("query")
    monkeypatch.setattr("app.retrieval.embedding._load_model", lambda config: SimpleNamespace(
        encode=lambda texts, **kwargs: {"dense_vecs": [vector()]}
    ))
    assert service.embed_query("query") == vector()


@pytest.mark.parametrize("texts", [[""], [" "], [None]])
def test_empty_embedding_inputs_rejected(texts):
    with pytest.raises(ValueError):
        BGEEmbeddingService().embed_documents(texts)


def test_loader_downloads_only_inference_files_and_loads_local_snapshot(monkeypatch, tmp_path):
    from unittest.mock import Mock

    download = Mock(return_value=str(tmp_path))
    constructor = Mock(return_value="loaded-model")
    monkeypatch.setattr("huggingface_hub.snapshot_download", download)
    monkeypatch.setattr("app.retrieval.native_models.NativeEncoder", constructor)
    config = Settings(database_url="sqlite://", embedding_local_files_only=True)
    assert _load_model(config) == "loaded-model"
    assert download.call_args.kwargs["local_files_only"] is True
    patterns = download.call_args.kwargs["allow_patterns"]
    assert "pytorch_model.bin" not in patterns and "model.safetensors" in patterns and "tokenizer.json" in patterns
    assert all("onnx" not in pattern and pattern != "*" for pattern in patterns)
    assert constructor.call_args.args == (str(tmp_path), "cpu", False)
    download.reset_mock()
    local_config = Settings(database_url="sqlite://", embedding_model=str(tmp_path))
    assert _load_model(local_config) == "loaded-model"
    download.assert_not_called()
