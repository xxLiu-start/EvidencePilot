"""Strict frozen research-task inputs, separate from the Day 3 rank benchmark."""
from collections import Counter
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.retrieval.chunking import DocumentBlock, chunk_blocks

CATEGORIES = {"single_source": 4, "multi_source": 4, "multi_hop": 4, "source_conflict": 3, "outdated": 3, "unanswerable": 2}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_chunks(sources):
    chunks = []
    for page in sources:
        document_id = str(uuid5(NAMESPACE_URL, page["url"] + page["content_hash"]))
        for chunk in chunk_blocks([DocumentBlock(page["body"], section=page["title"])],
                                  document_id=document_id, source=page["url"], version=page["content_hash"][:12]):
            chunks.append({"id": str(uuid5(NAMESPACE_URL, document_id + ":" + str(chunk.chunk_index))),
                           "document_id": document_id, "source_id": page["source_id"], "text": chunk.text,
                           "token_count": chunk.token_count, "chunk_index": chunk.chunk_index,
                           "source": page["url"], "version": chunk.version, "section": chunk.section})
    return chunks


def load_research_dataset(directory):
    directory = Path(directory)
    manifest = json.loads((directory/"manifest.json").read_text())
    if not manifest.get("files"):
        raise ValueError("No sealed manifest")
    for filename, expected in manifest["files"].items():
        if Path(filename).name != filename or sha(directory/filename) != expected:
            raise ValueError("Frozen dataset hash mismatch")
    sources = json.loads((directory/"sources.json").read_text())["sources"]
    tasks = json.loads((directory/"tasks.json").read_text())["tasks"]
    pages = {page["source_id"]: page for page in sources}
    if len(pages) != len(sources) or len({task["task_id"] for task in tasks}) != len(tasks):
        raise ValueError("Duplicate source/task ID")
    if Counter(task["category"] for task in tasks) != CATEGORIES:
        raise ValueError("Require all 20 fixed task categories")
    for page in sources:
        if hashlib.sha256(page["body"].encode()).hexdigest() != page["content_hash"]:
            raise ValueError("Source body hash mismatch")
    for task in tasks:
        if not task["required_claims"] or not set(task["allowed_source_ids"]) <= pages.keys():
            raise ValueError("Invalid claim/source set")
        ids = [claim["id"] for claim in task["required_claims"]]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate required claim ID")
        for claim in task["required_claims"]:
            if claim["expected_status"] == "SUPPORTED" and not claim["reference_evidence"]:
                raise ValueError("Supported labels require independent reference evidence")
            for ref in claim["reference_evidence"]:
                page = pages[ref["source_id"]]
                if (ref["source_url"] != page["url"] or ref["source_id"] not in task["allowed_source_ids"]
                        or page["body"][ref["quote_start"]:ref["quote_end"]] != ref["supporting_text"]):
                    raise ValueError("Invalid reference quote/source binding")
    chunks = make_chunks(sources)
    if (directory/"chunks.json").exists() and json.loads((directory/"chunks.json").read_text())["chunks"] != chunks:
        raise ValueError("Frozen chunker output changed; version the dataset explicitly")
    return manifest, sources, tasks, chunks
