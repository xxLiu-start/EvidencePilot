"""Verify all six ingestion/retrieval criteria using real BGE-M3 and API restarts.

Run from the repository root with EVIDENCEPILOT_TEST_POSTGRES_URL set.
Only a disposable schema is created; existing documents are not changed.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

# Allow `python scripts/acceptance.py` without installing the repository.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import numpy as np
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk
from app.db.session import Base
from app.retrieval.chunking import get_tokenizer


def make_fixtures(directory: Path) -> list[Path]:
    markdown = directory / "retrieval.md"
    markdown.write_text(
        "# Retrieval\n\n" + (
            "PostgreSQL uses pgvector to store document embeddings and find related chunks "
            "with cosine similarity search. "
        ) * 100 + "\n\n# Cooking\n\nPasta is cooked in boiling water.", encoding="utf-8",
    )
    docx = directory / "manual.docx"
    document = Document()
    document.add_heading("Embedding", level=1)
    document.add_paragraph("BGE-M3 produces 1024 dimensional multilingual document embeddings.")
    document.add_heading("Gardening", level=1)
    document.add_paragraph("Tomato plants need sunshine and regular watering.")
    document.save(docx)
    pdf = directory / "guide.pdf"
    writer = PdfWriter()
    for lines in [
        ["1 Vector search", "PostgreSQL pgvector supports cosine similarity search.",
         "Document embeddings are stored in a persistent database."],
        ["2 Astronomy", "The moon orbits Earth and reflects sunlight."],
    ]:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        stream = DecodedStreamObject()
        commands = ["BT /F1 12 Tf 50 700 Td"]
        for line in lines:
            commands.extend([f"({line}) Tj", "0 -20 Td"])
        stream.set_data(("\n".join(commands) + "\nET").encode("ascii"))
        page[NameObject("/Contents")] = stream
    writer.write(pdf)
    return [pdf, docx, markdown]


class Server:
    def __init__(self, database_url: str, directory: Path):
        self.database_url = database_url
        self.directory = directory
        self.process = None
        self.log = None
        self.client = None
        self.pids: list[int] = []

    def start(self) -> httpx.Client:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {
            **os.environ, "DATABASE_URL": self.database_url,
            "EMBEDDING_LOCAL_FILES_ONLY": "true", "HF_HUB_OFFLINE": "1",
            "EMBEDDING_DEVICE": "cpu", "EMBEDDING_USE_FP16": "false",
            "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
            "TOKENIZERS_PARALLELISM": "false", "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        }
        self.log = (self.directory / f"server-{len(self.pids)}.log").open("w")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port)], cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.pids.append(self.process.pid)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=180)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("API failed to start; check the database and model configuration")
            try:
                if self.client.get("/health").status_code == 200:
                    return self.client
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise RuntimeError("API health check timed out")

    def stop(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log is not None:
            self.log.close()


def wait_for_task(client, task_id, timeout=600):
    """New tasks run via BackgroundTasks; poll without racing the worker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        if response.json()["status"] in {"completed", "partial", "failed"}:
            response = client.get(f"/api/v1/tasks/{task_id}/agent")
            response.raise_for_status()
            return response
        time.sleep(.5)
    raise TimeoutError("Background research task timed out")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "data/cache/acceptance/latest.json")
    args = parser.parse_args()
    url = os.environ.get("EVIDENCEPILOT_TEST_POSTGRES_URL")
    if not url:
        parser.error("Set EVIDENCEPILOT_TEST_POSTGRES_URL to a PostgreSQL test database")
    report = {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "status": "running", "checks": [],
    }

    def passed(name: str, details) -> None:
        report["checks"].append({"name": name, "passed": True, "details": details})
        print(f"PASS {name}", flush=True)

    admin = create_engine(url)
    scoped = None
    schema = "ep_acceptance_" + uuid4().hex
    try:
        with admin.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema},public"})
        scoped = create_engine(scoped_url)
        with scoped.begin() as conn:
            Base.metadata.create_all(conn, checkfirst=False)
        with TemporaryDirectory(prefix="evidencepilot-acceptance-") as directory:
            paths = make_fixtures(Path(directory))
            server = Server(scoped_url.render_as_string(hide_password=False), Path(directory))
            try:
                client = server.start()
                documents, chunks_by_type = {}, {}
                for path in paths:
                    with path.open("rb") as file:
                        response = client.post("/api/v1/documents", files={"file": (path.name, file)},
                                               data={"chunk_size": 160, "chunk_overlap": 24,
                                                     "version": "acceptance-v1"})
                    assert response.status_code == 201, response.text
                    document = response.json()
                    chunks_response = client.get(f"/api/v1/documents/{document['id']}/chunks")
                    assert chunks_response.status_code == 200, chunks_response.text
                    chunks = chunks_response.json()
                    assert chunks and all(c["text"].strip() for c in chunks)
                    assert all(c["source"] == path.name and c["version"] == "acceptance-v1" for c in chunks)
                    documents[path.suffix] = document
                    chunks_by_type[path.suffix] = chunks
                assert [c["page"] for c in chunks_by_type[".pdf"]] == [1, 2]
                assert "Embedding" in {c["section"] for c in chunks_by_type[".docx"]}
                passed("parse_pdf_docx_markdown", {k: len(v) for k, v in chunks_by_type.items()})

                chunks = chunks_by_type[".md"]
                assert all(0 < c["token_count"] <= 160 for c in chunks)
                assert any(c["token_count"] == 160 for c in chunks)
                encoding = get_tokenizer()
                overlap_pairs = 0
                for previous, current in zip(chunks, chunks[1:]):
                    if previous["section"] == current["section"]:
                        suffix = encoding.decode(encoding.encode(previous["text"])[-24:])
                        assert current["text"].startswith(suffix)
                        overlap_pairs += 1
                assert overlap_pairs > 0
                passed("configured_chunking", {"chunk_size": 160, "overlap": 24, "overlap_pairs": overlap_pairs})

                with Session(scoped) as db:
                    stored = list(db.scalars(select(DocumentChunk)))
                    vectors = np.asarray([c.embedding for c in stored])
                    assert vectors.shape == (len(stored), 1024) and np.isfinite(vectors).all()
                    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, rtol=1e-5)
                    passed("bge_m3_1024_dimensions", {"model": stored[0].embedding_model, "dimensions": 1024})
                    expected_count = sum(d["chunk_count"] for d in documents.values())
                    assert len(stored) == expected_count
                    stored_ids = {str(c.id) for c in stored}
                    passed("chunks_and_vectors_in_postgresql", {"stored_chunks": len(stored)})

                query = "How does PostgreSQL pgvector perform vector similarity search?"
                response = client.post("/api/v1/search", json={"query": query})
                assert response.status_code == 200, response.text
                before = response.json()["results"]
                assert len(before) == 5
                assert all(before[i]["score"] >= before[i + 1]["score"] for i in range(4))
                assert "pgvector" in before[0]["text"].lower()
                assert all(c["id"] in stored_ids for c in before)
                passed("top_five_related_chunks", [{"id": c["id"], "score": c["score"], "source": c["source"]} for c in before])

                server.stop()
                client = server.start()
                response = client.post("/api/v1/search", json={"query": query})
                assert response.status_code == 200, response.text
                after = response.json()["results"]
                assert len(after) == 5 and all(c["id"] in stored_ids for c in after)
                assert "pgvector" in after[0]["text"].lower()
                assert abs(after[0]["score"] - before[0]["score"]) < 1e-5
                assert server.pids[0] != server.pids[1]
                with Session(scoped) as db:
                    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == expected_count
                passed("retrieval_after_service_restart", {"processes": server.pids, "results": len(after)})
                report["status"] = "passed"
            finally:
                server.stop()
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        if scoped is not None:
            scoped.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Acceptance report: {args.report}", flush=True)


if __name__ == "__main__":
    main()
