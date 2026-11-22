from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk, DocumentRecord
from app.db.session import Base
from app.retrieval.bm25 import BM25Store, bm25_retrieve, build_bm25_index, tokenize


def test_bm25_english_chinese_no_matches_and_empty_corpus():
    chunks = [
        {"id": "a", "content": "PostgreSQL 数据库支持向量检索"},
        {"id": "b", "content": "Tomato plants need sunshine"},
        {"id": "c", "content": "Pasta cooking recipes"},
    ]
    index = build_bm25_index(chunks)
    assert bm25_retrieve(index, chunks, "POSTGRESQL，数据库")[0]["id"] == "a"
    assert bm25_retrieve(index, chunks, "tomato")[0]["id"] == "b"
    assert bm25_retrieve(index, chunks, "unmatched") == []
    assert bm25_retrieve(index, chunks, "!!!") == []
    assert build_bm25_index([]) is None
    assert build_bm25_index([{"id": "x", "text": "!!!"}]) is None
    assert "bge-m3" in tokenize("BGE-M3")
    assert "数据库" in tokenize("数据库检索")
    with pytest.raises(ValueError):
        bm25_retrieve(index, chunks[:1], "tomato")


def test_two_worker_snapshots_refresh_after_insert_update_delete_and_rollback():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    first, second = BM25Store(), BM25Store()
    with Session(engine) as db:
        assert first.rebuild(db) == 0
        document = DocumentRecord(id=uuid4(), filename="guide", source="guide", version="1", chunk_count=1)
        db.add(document)
        db.flush()
        chunk = DocumentChunk(document_id=document.id, chunk_index=0, text="database search",
                              token_count=2, source="guide", version="1")
        db.add(chunk)
        db.commit()
        assert first.retrieve(db, "database")[0].chunk.id == chunk.id
        assert second.retrieve(db, "database")[0].chunk.id == chunk.id
        assert first.retrieve(db, "database", document_id=uuid4()) == []
        chunk.text = "中文数据库检索"
        db.commit()
        assert first.retrieve(db, "中文") and second.retrieve(db, "中文")
        chunk.text = "rolledbackword"
        db.flush()
        db.rollback()
        assert first.retrieve(db, "rolledbackword") == []
        assert first.retrieve(db, "数据库")
        db.delete(chunk)
        db.commit()
        assert first.retrieve(db, "数据库") == second.retrieve(db, "数据库") == []
    engine.dispose()
