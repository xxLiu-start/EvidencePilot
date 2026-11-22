"""Shared frozen retrieval backend for every research method. No network search."""
from datetime import datetime, timezone
import json
import importlib.metadata
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import select

from app.agent.tools import ToolRegistry, ToolResult
from app.db.models import (AgentAllowedSource, DocumentChunk, DocumentRecord, EvidenceAssessment,
                           WebContent, WebSource)
from app.evidence.extraction import extract_evidence, save_evidences
from app.evidence.schema import Evidence
from app.evaluation.research_dataset import sha
from app.evaluation.benchmark import model_revision
from app.retrieval.bm25 import tokenize
from app.retrieval.embedding import validate_embeddings
from app.search.cache import digest
from app.search.parser import ParsedPage
from app.search.query_generator import ClaimInput
from app.verification.verifier import assess_claim


def seed_frozen_corpus(db, sources, chunks, embedder, cache_path):
    cfg = embedder.config
    provenance = {"model": embedder.model_name,
        "revision": model_revision(embedder.model_name, cfg.embedding_cache_dir, cfg.embedding_revision),
        "max_length": cfg.embedding_max_length, "use_fp16": cfg.embedding_use_fp16,
        "versions": {name: importlib.metadata.version(name) for name in ("FlagEmbedding", "transformers", "torch")}}
    key = digest(json.dumps([[(chunk["id"], chunk["text"]) for chunk in chunks], provenance], ensure_ascii=False, sort_keys=True))
    cache = Path(cache_path) / (key + ".npz")
    if cache.exists():
        with np.load(cache, allow_pickle=False) as data:
            vectors = validate_embeddings(data["vectors"], len(chunks))
    else:
        vectors = validate_embeddings(embedder.embed_documents([chunk["text"] for chunk in chunks]), len(chunks))
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, vectors=np.asarray(vectors, dtype=np.float32))
    for source in sources:
        db.add(WebContent(content_hash=source["content_hash"], body=source["body"], cache_path="frozen-benchmark"))
    db.flush()
    by_source = {source["source_id"]: source for source in sources}
    for source in sources:
        db.add(WebSource(id=uuid5(NAMESPACE_URL, source["url"]), canonical_url=source["url"], fetched_url=source["fetched_url"], title=source["title"],
                        content_hash=source["content_hash"], published_at=datetime.fromisoformat(source["published_at"]) if source["published_at"] else None,
                        retrieved_at=datetime.fromisoformat(source["retrieved_at"]), source_type="web", search_queries=[]))
        rows = [chunk for chunk in chunks if chunk["source_id"] == source["source_id"]]
        db.add(DocumentRecord(id=UUID(rows[0]["document_id"]), filename=source["source_id"] + ".md",
                              source=source["url"], version=source["content_hash"][:12], chunk_count=len(rows)))
    db.flush()
    for chunk, vector in zip(chunks, vectors, strict=True):
        db.add(DocumentChunk(id=UUID(chunk["id"]), document_id=UUID(chunk["document_id"]),
            chunk_index=chunk["chunk_index"], text=chunk["text"], token_count=chunk["token_count"],
            source=chunk["source"], version=chunk["version"], section=chunk["section"],
            embedding=vector, embedding_model=embedder.model_name))
    db.commit()
    return {"vectors_sha256": sha(cache), "vector_count": len(vectors), "dimension": 1024}


class FrozenResearchTools:
    def __init__(self, sources, chunks, task, embedder, *, as_of):
        allowed = set(task["allowed_source_ids"])
        self.sources = {source["source_id"]: source for source in sources if source["source_id"] in allowed}
        self.chunks = {chunk["id"]: chunk for chunk in chunks if chunk["source_id"] in allowed}
        self.web_chunks = [chunk for chunk in self.chunks.values() if "web" in self.sources[chunk["source_id"]]["channels"]]
        self.bm25 = BM25Okapi([tokenize(chunk["text"]) for chunk in self.web_chunks])
        self.embedder, self.as_of = embedder, as_of
        self.seen, self.calls, self.last_web, self.evidence_to_chunk = {}, [], {}, {}

    def retrieve(self, db, query, top_k=5, channel="rag"):
        if len(self.calls) >= 6:
            raise ValueError("Shared retrieval budget exceeded")
        self.calls.append({"action": "SEARCH_WEB" if channel == "web" else "RETRIEVE_RAG", "query": query})
        if channel == "web":
            scores = self.bm25.get_scores(tokenize(query))
            ranked = sorted(zip(self.web_chunks, scores), key=lambda item: (-item[1], item[0]["id"]))
            result = [chunk for chunk, _ in ranked[:top_k]]
            for source_id in dict.fromkeys(chunk["source_id"] for chunk in result):
                self.last_web[source_id] = next(chunk for chunk in result if chunk["source_id"] == source_id)
        else:
            vector = self.embedder.embed_query(query)
            ids = [UUID(chunk["id"]) for chunk in self.chunks.values()
                   if "rag" in self.sources[chunk["source_id"]]["channels"]]
            distance = DocumentChunk.embedding.cosine_distance(vector)
            rows = db.scalars(select(DocumentChunk).where(DocumentChunk.id.in_(ids))
                .order_by(distance, DocumentChunk.id).limit(top_k))
            result = [self.chunks[str(row.id)] for row in rows]
        self.seen.update({chunk["id"]: chunk for chunk in result})
        return result

    def context(self, top_k=10, preferred=()):
        ordered = sorted(self.seen.values(), key=lambda chunk: (chunk["id"] not in set(preferred),))[:top_k]
        return [{"evidence_id": chunk["id"], "source_id": chunk["source_id"], "source_url": self.sources[chunk["source_id"]]["url"],
                 "published_at": self.sources[chunk["source_id"]]["published_at"],
                 "scope": self.sources[chunk["source_id"]]["scope"], "content": chunk["text"]} for chunk in ordered]

    def save_chunk_quotes(self, db, context, chunks):
        evidence = []
        for chunk in chunks:
            source = self.sources[chunk["source_id"]]
            quote = chunk["text"][:3000]
            offset = source["body"].index(quote)
            identifier = uuid5(NAMESPACE_URL, f"research:{context.task_id}:{context.claim['id']}:{chunk['id']}")
            self.evidence_to_chunk[str(identifier)] = chunk["id"]
            evidence.append(Evidence(evidence_id=identifier, claim_id=context.claim["id"], claim_text=context.claim["claim"],
                source_type="web", source_url=source["url"], document_id=UUID(chunk["document_id"]), chunk_id=UUID(chunk["id"]),
                published_at=source["published_at"], retrieved_at=source["retrieved_at"], supporting_text=quote, relation="context",
                content_hash=source["content_hash"], quote_start=offset, quote_end=offset+len(quote), scope=source["scope"] or None))
        result = ToolResult(ok=bool(evidence), evidence_ids=[str(item.evidence_id) for item in evidence], summary=f"Retrieved {len(evidence)} frozen official-source excerpts by Dense RAG.")
        context.record_result(db, result)
        save_evidences(db, evidence, before_commit=lambda: context.before_commit(db))
        return result

    def rag(self, db, context, query, top_k=5):
        context.check()
        chunks = self.retrieve(db, query, top_k, "rag")
        context.check()
        return self.save_chunk_quotes(db, context, chunks)

    def web(self, db, context, query):
        chunks = self.retrieve(db, query, 5, "web")
        source_ids = []
        for source_id in dict.fromkeys(chunk["source_id"] for chunk in chunks):
            source = db.scalar(select(WebSource).where(WebSource.canonical_url == self.sources[source_id]["url"]))
            if db.get(AgentAllowedSource, (context.task_id, source.id)) is None:
                db.add(AgentAllowedSource(task_id=context.task_id, source_id=source.id, claim_id=context.claim["id"]))
            source_ids.append(str(source.id))
        result = ToolResult(ok=bool(source_ids), source_ids=source_ids, summary=f"Frozen BM25 web cache returned {len(source_ids)} authorized source pages; OPEN_SOURCE extracts exact quotes.")
        context.record_result(db, result)
        db.commit()
        return result

    def open(self, db, context, url):
        source = next((page for page in self.sources.values() if page["url"] == url), None)
        row = db.scalar(select(WebSource).where(WebSource.canonical_url == url))
        if source is None or row is None or db.get(AgentAllowedSource, (context.task_id, row.id)) is None:
            raise ValueError("URL is not authorized by this task's frozen search")
        chunk = self.last_web.get(source["source_id"])
        if chunk is None:
            raise ValueError("No search window available")
        page = ParsedPage(url, source["fetched_url"], source["title"], source["body"], source["published_at"],
                          source["retrieved_at"], source["content_hash"], cache_hit=True)
        evidence, _ = extract_evidence(ClaimInput(claim_id=context.claim["id"], claim_text=context.claim["claim"]),
            page, client=context.client, max_evidences=2, timeout_seconds=context.remaining(), excerpt=chunk["text"])
        for item in evidence:
            self.evidence_to_chunk[str(item.evidence_id)] = chunk["id"]
            if item.scope is None and source["scope"]:
                from app.verification.scope import EvidenceScope
                item.scope = EvidenceScope(**source["scope"])
        result = ToolResult(ok=bool(evidence), source_ids=[str(row.id)], evidence_ids=[str(item.evidence_id) for item in evidence],
                            summary=f"Extracted {len(evidence)} exact quotes from a fixed cached window.")
        context.record_result(db, result)
        save_evidences(db, evidence, before_commit=lambda: context.before_commit(db))
        return result

    def verification_disabled(self, db, context, claim_id):
        """Explicitly defective ablation: provenance presence substitutes for entailment."""
        claim = {key: value for key, value in context.claim.items() if key != "evidence_ids"}
        state = assess_claim(db, claim, context.claim.get("evidence_ids", []), task_id=context.task_id,
                             config=context.config, now=self.as_of)
        if not state["verification_attempted"]:
            supports = state["qualified_evidence_ids"]
            result = {"claim_id": claim_id, "verdict": "SUPPORTED" if supports else "INSUFFICIENT",
                      "status": "SUPPORTED" if supports else "INSUFFICIENT", "verified": True,
                      "verification_method": "ablation_provenance_only_no_semantic_verifier",
                      "supporting_evidence_ids": supports, "refuting_evidence_ids": [], "contradicting_evidence_ids": [],
                      "reason": "Ablation only: bypassed semantic verification.", "error_category": None}
            db.add(EvidenceAssessment(task_id=context.task_id, claim_id=claim_id, fingerprint=state["fingerprint"],
                                       result=result, trace=None, created_at=datetime.now(timezone.utc)))
        result = ToolResult(ok=True, summary="Ablation: source-presence check substituted for semantic verification.")
        context.record_result(db, result)
        db.commit()
        return result

    def registry(self, verification=True):
        overrides = {"rag_retrieve_tool": self.rag, "web_search_tool": self.web, "open_source_tool": self.open}
        if not verification:
            overrides["verify_claim_tool"] = self.verification_disabled
        return ToolRegistry(overrides)
