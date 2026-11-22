"""Freeze official sources once. Labels are authored rules, pending human review.

This is the ONLY network acquisition step. Evaluation never refreshes these files.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import trafilatura
from app.search.cache import digest, write_json

DEST = ROOT / "data/benchmark/research_v1"
SOURCES = [
    ("bge", "https://huggingface.co/BAAI/bge-m3/raw/main/README.md", "BGE-M3 model card", None, ["rag", "web"]),
    ("reranker", "https://huggingface.co/BAAI/bge-reranker-v2-m3/raw/main/README.md", "BGE reranker v2 m3 model card", None, ["rag", "web"]),
    ("pgvector", "https://raw.githubusercontent.com/pgvector/pgvector/v0.8.0/README.md", "pgvector v0.8.0 README", None, ["rag", "web"]),
    ("fastapi", "https://fastapi.tiangolo.com/tutorial/background-tasks/", "FastAPI background tasks", None, ["rag", "web"]),
    ("langgraph", "https://docs.langchain.com/oss/python/langgraph/durable-execution", "LangGraph durable execution", None, ["web"]),
    ("pydantic", "https://raw.githubusercontent.com/pydantic/pydantic/v2.10.6/docs/migration.md", "Pydantic v2 migration", None, ["rag", "web"]),
    ("qwen7", "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct/raw/main/README.md", "Qwen2.5-7B-Instruct model card", None, ["web"]),
    ("qwen1", "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/raw/main/README.md", "Qwen2.5-1.5B-Instruct model card", None, ["rag", "web"]),
    ("pgasync", "https://www.postgresql.org/docs/16/wal-async-commit.html", "PostgreSQL 16 asynchronous commit", None, ["rag", "web"]),
    ("pgwal", "https://www.postgresql.org/docs/16/runtime-config-wal.html", "PostgreSQL 16 WAL configuration", None, ["rag", "web"]),
    ("oldvector", "https://raw.githubusercontent.com/pgvector/pgvector/v0.7.0/README.md", "pgvector v0.7.0 historical README", "2024-04-29T00:00:00+00:00", ["rag", "web"]),
    ("oldqwen", "https://qwenlm.github.io/blog/qwen2.5/", "Qwen2.5 release announcement (2024)", "2024-09-19T00:00:00+00:00", ["rag", "web"]),
]


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    if (DEST / "sources.json").exists():
        print("Frozen sources already exist; refusing to silently refresh them.")
        return
    pages = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for identifier, url, title, published, channels in SOURCES:
            response = client.get(url)
            response.raise_for_status()
            raw = response.text
            body = trafilatura.extract(raw, include_tables=True) if "text/html" in response.headers.get("content-type", "") else raw
            if not body or len(body) < 100:
                raise ValueError("No extractable source body: " + identifier)
            raw_path = DEST / "raw" / (identifier + ".txt")
            raw_path.parent.mkdir(exist_ok=True)
            raw_path.write_text(raw, encoding="utf-8")
            scope = ({"model_name": "Qwen2.5-7B-Instruct", "model_version": "2.5", "deployment_mode": "local"} if identifier == "qwen7" else
                     {"model_name": "Qwen2.5-1.5B-Instruct", "model_version": "2.5", "deployment_mode": "local"} if identifier == "qwen1" else {})
            pages.append({"source_id": identifier, "url": url, "fetched_url": str(response.url), "title": title,
                          "body": body, "content_hash": digest(body), "raw_hash": digest(raw), "published_at": published,
                          "retrieved_at": datetime.now(timezone.utc).isoformat(), "source_class": "official",
                          "channels": channels, "scope": scope})
            print("FROZEN", identifier, len(body), flush=True)
    write_json(DEST / "sources.json", {"sources": pages})
    print("Review source excerpts, then create tasks and seal manifest.", flush=True)


if __name__ == "__main__":
    main()
