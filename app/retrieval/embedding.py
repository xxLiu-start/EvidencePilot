"""Lazy, reusable BGE-M3 dense embedding service."""

from functools import lru_cache
import logging
from pathlib import Path
from threading import Lock

import numpy as np

from app.config import Settings, settings
from app.retrieval.model_integrity import verified_model_path


logger = logging.getLogger(__name__)
BGE_M3_DIMENSIONS = 1024


class EmbeddingError(RuntimeError):
    """Model loading, inference or vector validation failed."""


def validate_embeddings(vectors, expected_count: int) -> list[list[float]]:
    """Reject corrupt or incompatible output before starting a database write."""
    try:
        array = np.asarray(vectors, dtype=np.float32)
        if array.shape != (expected_count, BGE_M3_DIMENSIONS):
            raise ValueError("Unexpected embedding count or dimension")
        if not np.isfinite(array).all():
            raise ValueError("Embeddings contain non-finite values")
        norms = np.linalg.norm(array.astype(np.float64), axis=1, keepdims=True)
        if (norms == 0).any():
            raise ValueError("Embeddings contain zero vectors")
        return (array / norms).astype(np.float32).tolist()
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingError("Invalid BGE-M3 embedding output") from exc


def _load_model(config: Settings):
    # Avoid importing torch/transformers or downloading weights on API startup.
    from FlagEmbedding import BGEM3FlagModel

    model_path = verified_model_path(config, "embedding")
    if not Path(model_path).is_dir():
        from huggingface_hub import snapshot_download

        # FlagEmbedding otherwise downloads the entire repository, including
        # large ONNX exports that are not used by this PyTorch encoder.
        model_path = snapshot_download(
            config.embedding_model, revision=config.embedding_revision, cache_dir=config.embedding_cache_dir,
            local_files_only=config.embedding_local_files_only,
            allow_patterns=[
                "config.json", "pytorch_model.bin", "pytorch_model.bin.index.json",
                "pytorch_model-*.bin", "model.safetensors", "model.safetensors.index.json",
                "model-*.safetensors", "tokenizer.json", "tokenizer_config.json",
                "special_tokens_map.json", "sentencepiece.bpe.model",
                "sparse_linear.pt", "colbert_linear.pt",
            ],
        )
    return BGEM3FlagModel(
        model_path,
        devices=config.embedding_device,
        use_fp16=config.embedding_use_fp16,
        normalize_embeddings=True,
        cache_dir=config.embedding_cache_dir,
    )


class BGEEmbeddingService:
    def __init__(self, config: Settings = settings):
        self.config = config
        self.model_name = config.embedding_model
        self._model = None
        # FlagEmbedding may move the model between devices during encode.
        self._lock = Lock()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding inputs must be non-empty strings")
        try:
            with self._lock:
                if self._model is None:
                    self._model = _load_model(self.config)
                vectors = []
                # Bound inference and validation memory even for large documents.
                size = self.config.embedding_batch_size
                for start in range(0, len(texts), size):
                    batch = texts[start:start + size]
                    output = self._model.encode(
                        batch, batch_size=size, max_length=self.config.embedding_max_length,
                        return_dense=True, return_sparse=False, return_colbert_vecs=False,
                    )
                    vectors.extend(validate_embeddings(output["dense_vecs"], len(batch)))
                return vectors
        except EmbeddingError:
            raise
        except Exception as exc:
            logger.exception("BGE-M3 model loading or inference failed")
            raise EmbeddingError("BGE-M3 embedding service is unavailable") from exc

    def embed_query(self, text: str) -> list[float]:
        # BGE-M3 uses the same encoder without a retrieval instruction prefix.
        return self.embed_documents([text])[0]


@lru_cache(maxsize=1)
def get_embedding_service() -> BGEEmbeddingService:
    return BGEEmbeddingService()
