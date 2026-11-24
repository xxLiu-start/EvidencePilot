"""Cache isolation tests: identical URLs/hash, search snapshots and ranking corpus."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import ClaimRecord, DocumentChunk, DocumentRecord, WebContent, WebSource
from app.db.session import Base, get_cached_content
from app.retrieval.bm25 import BM25Store
from app.search.cache import CacheMiss, digest, scoped_cache_config, scoped_cache_root
from app.search.parser import ParsedPage, fetch_page
from app.search.web_search import WebSearchClient


def _scope(db, tenant, project):
    db.info.update(tenant_id=tenant, project_ids=(project,),
                   selected_project_id=project, selected_project_ids=(project,))


def test_disk_cache_scope_never_falls_back_to_legacy_or_foreign_tenant(tmp_path):
    a, b, p, q = uuid4(), uuid4(), uuid4(), uuid4()
    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path))
    sdk = SimpleNamespace(search=lambda **kw: {"results": [{"url": "https://example.org/"}]})
    info_a = {"tenant_id": a, "project_ids": (p,)}
    info_b = {"tenant_id": b, "project_ids": (q,)}
    legacy = WebSearchClient(cfg, sdk=sdk)
    legacy.search("same confidential query")
    scoped_a = WebSearchClient(scoped_cache_config(SimpleNamespace(info=info_a), cfg), sdk=sdk)
    scoped_b = WebSearchClient(scoped_cache_config(SimpleNamespace(info=info_b), cfg), sdk=sdk)
    with pytest.raises(CacheMiss):
        scoped_a.search("same confidential query", cache_only=True)
    scoped_a.search("same confidential query")
    assert scoped_a.search("same confidential query", cache_only=True)["cache_hit"]
    with pytest.raises(CacheMiss):
        scoped_b.search("same confidential query", cache_only=True)
    assert scoped_cache_root(tmp_path, info_a) != scoped_cache_root(tmp_path, info_b)
    with pytest.raises(PermissionError):
        scoped_cache_root(tmp_path, {}, production=True)
    with pytest.raises(PermissionError):
        scoped_cache_root(tmp_path, {"tenant_id": a, "project_ids": (p, q)})
    with pytest.raises(ValueError):
        scoped_cache_root(tmp_path, {"tenant_id": "../private", "project_ids": (p,)})


def test_sql_cache_same_url_and_hash_are_separate_across_tenants(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    a, b, p, q = uuid4(), uuid4(), uuid4(), uuid4()
    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path))
    body = "same body, separate cache ownership"
    url = "https://example.org/"
    now = datetime.now(timezone.utc).isoformat()
    downloads = []
    def download(canonical, config):
        downloads.append(str(config.web_cache_dir))
        return ParsedPage(canonical, canonical, "Source", body, now, now, digest(body))
    monkeypatch.setattr("app.search.parser.download_page", download)
    try:
        with Session(engine) as db:
            _scope(db, a, p)
            db.add(ClaimRecord(claim_id="C1", claim_text="Same claim"))
            db.commit()
            fetch_page(db, url, search_query="private query A", config=cfg)
            assert get_cached_content(db, digest(body)).cache_scope == str(a)
        with Session(engine) as db:
            _scope(db, b, q)
            assert get_cached_content(db, digest(body)) is None
            assert db.scalar(select(WebSource)) is None
            assert db.scalar(select(ClaimRecord)) is None
            with pytest.raises(CacheMiss):
                fetch_page(db, url, cache_only=True, config=cfg)
            db.add(ClaimRecord(claim_id="C1", claim_text="Same claim"))
            db.commit()
            fetch_page(db, url, search_query="private query B", config=cfg)
            assert db.scalar(select(WebSource)).search_queries == ["private query B"]
        with Session(engine) as db:
            _scope(db, a, p)
            assert db.scalar(select(WebSource)).search_queries == ["private query A"]
            db.add(WebContent(cache_scope=str(b), content_hash="f" * 64, body="denied", cache_path=""))
            with pytest.raises(PermissionError):
                db.flush()
            db.rollback()
        assert len(downloads) == 2 and downloads[0] != downloads[1]
    finally:
        engine.dispose()


def test_bm25_builds_independent_corpora_before_ranking():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    a, b, p, q = uuid4(), uuid4(), uuid4(), uuid4()
    store = BM25Store()
    try:
        for tenant, project, body in ((a, p, "shared alpha"), (b, q, "shared beta")):
            with Session(engine) as db:
                _scope(db, tenant, project)
                doc = DocumentRecord(filename="doc", source="test", version="1", chunk_count=1)
                db.add(doc)
                db.flush()
                db.add(DocumentChunk(document_id=doc.id, chunk_index=0, text=body,
                                     token_count=2, source="test", version="1"))
                db.commit()
        with Session(engine) as db:
            _scope(db, a, p)
            assert [hit.chunk.text for hit in store.retrieve(db, "shared")] == ["shared alpha"]
            assert store._snapshot(db)[0].corpus_size == 1
        with Session(engine) as db:
            _scope(db, b, q)
            assert [hit.chunk.text for hit in store.retrieve(db, "shared")] == ["shared beta"]
            assert store._snapshot(db)[0].corpus_size == 1
    finally:
        engine.dispose()
