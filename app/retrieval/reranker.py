"""Lazy BGE Cross-Encoder scoring with bounded batches and validated outputs."""

from dataclasses import replace
from functools import lru_cache
import logging
from pathlib import Path
from threading import Lock

import numpy as np

from app.config import Settings, settings
from app.retrieval.model_integrity import verified_model_path
from app.retrieval.remote import request_inference
from app.retrieval.types import item_id, item_text


logger = logging.getLogger(__name__)


class RerankerError(RuntimeError):
    pass


def _load_reranker(config):
    from app.retrieval.native_models import NativeEncoder

    path = verified_model_path(config, "reranker")
    if not Path(path).is_dir():
        from huggingface_hub import snapshot_download
        path = snapshot_download(
            path, revision=config.reranker_revision, cache_dir=config.reranker_cache_dir,
            local_files_only=config.reranker_local_files_only,
            allow_patterns=["config.json", "model.safetensors", "model.safetensors.index.json",
                            "model-*.safetensors", "tokenizer.json", "tokenizer_config.json",
                            "special_tokens_map.json", "sentencepiece.bpe.model", "tokenizer.model",
                            "vocab.txt", "vocab.json", "merges.txt"],
        )
    return NativeEncoder(path, config.reranker_device, config.reranker_use_fp16, rerank=True)


class RerankerService:
    def __init__(self, config: Settings = settings):
        self.config = config
        self.model_name = config.reranker_model
        self._model = None
        self._lock = Lock()

    def score(self, query, candidates):
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not candidates:
            return []
        if any(not item_text(item).strip() for item in candidates):
            raise ValueError("Candidate content must be non-empty")
        try:
            if self.config.inference_mode == "remote":
                scores = []
                size = min(self.config.reranker_batch_size, 32)
                for start in range(0, len(candidates), size):
                    batch = candidates[start:start + size]
                    output = request_inference(self.config, "/v1/rerank",
                                               {"query": query, "texts": [item_text(item) for item in batch]}, "reranker")
                    values = np.asarray(output, dtype=np.float64)
                    if (values.shape != (len(batch),) or not np.isfinite(values).all()
                            or ((values < 0) | (values > 1)).any()):
                        raise RerankerError("Invalid remote reranker scores")
                    scores.extend(values.tolist())
                return scores
            with self._lock:
                if self._model is None:
                    self._model = _load_reranker(self.config)
                scores = []
                size = self.config.reranker_batch_size
                for start in range(0, len(candidates), size):
                    batch = candidates[start:start + size]
                    output = self._model.compute_score(
                        [[query, item_text(item)] for item in batch],
                        batch_size=size, max_length=self.config.reranker_max_length, normalize=True,
                    )
                    values = np.asarray(output, dtype=np.float64).reshape(-1)
                    if (len(values) != len(batch) or not np.isfinite(values).all()
                            or ((values < 0) | (values > 1)).any()):
                        raise RerankerError("Invalid reranker score output")
                    scores.extend(values.tolist())
                return scores
        except RerankerError:
            raise
        except Exception as exc:
            logger.exception("Cross-Encoder loading or inference failed")
            raise RerankerError("Cross-Encoder service unavailable") from exc

    def ranked(self, query, candidates, top_k=5):
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        pairs = zip(candidates, self.score(query, candidates), strict=True)
        return sorted(pairs, key=lambda pair: (-pair[1], item_id(pair[0])))[:top_k]

    def rerank_hits(self, query, candidates, top_k=5):
        return [replace(hit, score=score, reranker_score=score)
                for hit, score in self.ranked(query, candidates, top_k)]


@lru_cache(maxsize=1)
def get_reranker_service():
    return RerankerService()


def rerank(query, candidates, top_k=5):
    return [item for item, score in get_reranker_service().ranked(query, candidates, top_k)]
