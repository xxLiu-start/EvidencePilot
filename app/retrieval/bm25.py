"""Chinese/English BM25 with revision-aware PostgreSQL snapshots."""

from collections import OrderedDict
from collections.abc import Sequence
from functools import lru_cache
import re
from threading import Lock
from weakref import WeakKeyDictionary

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk, RetrievalIndexRevision
from app.retrieval.types import ChunkSnapshot, SearchHit, item_id, item_text


@lru_cache(maxsize=1)
def _tokenizer():
    tokenizer = jieba.Tokenizer()
    tokenizer.initialize()
    return tokenizer


def tokenize(text: str) -> list[str]:
    # Split punctuation consistently while retaining model names and Chinese words.
    segments = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+", text.casefold())
    return [token for segment in segments for token in (
        _tokenizer().cut(segment, HMM=False) if re.search(r"[\u3400-\u9fff]", segment) else [segment]
    ) if token.strip()]


def build_bm25_index(chunks: Sequence) -> BM25Okapi | None:
    tokenized = [tokenize(item_text(chunk)) for chunk in chunks]
    return BM25Okapi(tokenized) if any(tokenized) else None


def bm25_ranked(index, chunks: Sequence, query: str, top_k: int = 10, document_id=None,
                tenant_id=None, project_ids=None):
    if top_k <= 0 or not query.strip():
        raise ValueError("query and top_k must be non-empty/positive")
    if index is None:
        return []
    if index.corpus_size != len(chunks):
        raise ValueError("BM25 index does not match its chunk snapshot")
    tokens = tokenize(query)
    if not tokens:
        return []
    scores = index.get_scores(tokens)
    matches = []
    for i, (chunk, score) in enumerate(zip(chunks, scores, strict=True)):
        doc_id = chunk.get("document_id") if isinstance(chunk, dict) else chunk.document_id
        chunk_tenant = chunk.get("tenant_id") if isinstance(chunk, dict) else getattr(chunk, "tenant_id", None)
        chunk_project = chunk.get("project_id") if isinstance(chunk, dict) else getattr(chunk, "project_id", None)
        if document_id is not None and str(doc_id) != str(document_id):
            continue
        if tenant_id is not None and str(chunk_tenant) != str(tenant_id):
            continue
        if project_ids is not None and str(chunk_project) not in {str(value) for value in project_ids}:
            continue
        # No lexical match must not return arbitrary zero-score documents.
        if any(token in index.doc_freqs[i] for token in tokens):
            matches.append((chunk, float(score)))
    return sorted(matches, key=lambda pair: (-pair[1], item_id(pair[0])))[:top_k]


def bm25_retrieve(index, chunks, query, top_k=10):
    return [chunk for chunk, score in bm25_ranked(index, chunks, query, top_k)]


class BM25Store:
    def __init__(self):
        self._lock = Lock()
        self._snapshots = OrderedDict()
        self._max_scopes = 32

    def _snapshot(self, db: Session, force: bool = False):
        tenant_id = db.info.get("tenant_id")
        project_ids = tuple(db.info.get("selected_project_ids") or db.info.get("project_ids") or ())
        scope = (str(tenant_id), tuple(sorted(str(value) for value in project_ids)))
        with self._lock:
            revision = db.scalar(select(RetrievalIndexRevision.revision).where(
                RetrievalIndexRevision.id == 1
            )) or 0
            previous = self._snapshots.get(scope)
            if force or previous is None or previous[0] != revision:
                columns = [column for column in DocumentChunk.__table__.columns if column.name != "embedding"]
                dimension = case((DocumentChunk.embedding.is_not(None), 1024), else_=None)
                statement = select(*columns, dimension.label("embedding_dimensions")).order_by(DocumentChunk.id)
                # Scope SQL before constructing the index; post-ranking filtering
                # alone exposes other tenants' corpus statistics.
                if tenant_id is not None:
                    statement = statement.where(DocumentChunk.tenant_id == tenant_id)
                    statement = statement.where(DocumentChunk.project_id.in_(project_ids))
                rows = db.execute(statement)
                chunks = tuple(ChunkSnapshot(**row._mapping) for row in rows)
                index = build_bm25_index(chunks)
                self._snapshots[scope] = (revision, index, chunks)
                if len(self._snapshots) > self._max_scopes:
                    self._snapshots.popitem(last=False)
            self._snapshots.move_to_end(scope)
            _, index, chunks = self._snapshots[scope]
            return index, chunks

    def rebuild(self, db: Session) -> int:
        _, chunks = self._snapshot(db, force=True)
        return len(chunks)

    def retrieve(self, db: Session, query: str, *, top_k=10, document_id=None) -> list[SearchHit]:
        index, chunks = self._snapshot(db)
        return [SearchHit(chunk, score, bm25_score=score) for chunk, score in bm25_ranked(
            index, chunks, query, top_k=top_k, document_id=document_id,
            tenant_id=getattr(db, "info", {}).get("tenant_id"),
            project_ids=getattr(db, "info", {}).get("selected_project_ids")
            or getattr(db, "info", {}).get("project_ids"),
        )]


_stores = WeakKeyDictionary()
_stores_lock = Lock()


def get_bm25_store(bind) -> BM25Store:
    engine = getattr(bind, "engine", bind)
    with _stores_lock:
        if engine not in _stores:
            _stores[engine] = BM25Store()
        return _stores[engine]
