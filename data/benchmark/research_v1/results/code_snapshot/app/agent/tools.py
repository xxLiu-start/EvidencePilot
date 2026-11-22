"""The only executable tool whitelist. Workers use isolated SQL sessions."""

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
from threading import Event
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select

from app.config import Settings, settings
from app.db.models import AgentAllowedSource, EvidenceRecord, ToolExecutionReceipt, WebContent, WebSource
from app.memory.checkpoints import fence_tool_commit
from app.evidence.extraction import extract_evidence, save_evidences
from app.evidence.schema import Evidence
from app.retrieval.dense import search_chunks
from app.retrieval.hybrid import hybrid_retrieve
from app.search.cache import digest
from app.search.parser import ParsedPage, PageError, fetch_page, normalize_url
from app.search.query_generator import ClaimInput
from app.search.web_search import WebSearchClient


ToolName = Literal["web_search_tool", "rag_retrieve_tool", "open_source_tool", "verify_claim_tool"]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=600)]


def allowed_url_syntax(url):
    try:
        canonical = normalize_url(url)
    except PageError:
        raise ValueError("Only HTTP/HTTPS source URLs are allowed") from None
    host = urlsplit(canonical).hostname
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Private source target is forbidden")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private source target is forbidden")
    return canonical


class WebSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Query


class RAGInput(WebSearchInput):
    top_k: int = Field(default=5, ge=1, le=10)


class OpenSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=2048)]

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        return allowed_url_syntax(value)


class VerifyClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(pattern=r"^C[1-9][0-9]{0,2}$")


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: ToolName
    claim_id: str = Field(pattern=r"^C[1-9][0-9]{0,2}$")
    arguments: dict
    reason: str = Field(default="", max_length=300)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    summary: str = Field(max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    chunk_ids: list[str] = Field(default_factory=list, max_length=10)
    error_category: str | None = None


class ToolDeadline(RuntimeError):
    pass


@dataclass
class ToolContext:
    task_id: UUID
    claim: dict
    client: object
    deadline: float
    cancelled: Event
    config: Settings = None
    step_id: UUID | None = None
    owner_token: str | None = None
    reference_time: datetime | None = None

    def __post_init__(self):
        self.config = self.config or settings

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise ToolDeadline("Tool deadline exceeded")

    def remaining(self):
        self.check()
        return max(.01, self.deadline - time.monotonic())

    def before_commit(self, db):
        self.check()
        fence_tool_commit(db, self.task_id, self.owner_token, self.config)

    def record_result(self, db, result):
        self.before_commit(db)
        if self.step_id:
            row = db.get(ToolExecutionReceipt, self.step_id)
            if row is None:
                db.add(ToolExecutionReceipt(step_id=self.step_id, result=result.model_dump(mode="json"),
                                           created_at=datetime.now(timezone.utc)))


def web_search_tool(db, context: ToolContext, query: str):
    context.check()
    config = context.config.model_copy(update={
        "search_timeout_seconds": min(context.config.search_timeout_seconds, context.remaining()),
        "search_max_retries": 0,
    })
    searcher = WebSearchClient(config)
    try:
        results = searcher.search(query, max_results=2)
    finally:
        sdk = searcher._sdk
        if sdk is not None and hasattr(sdk, "session"):
            sdk.session.close()
    source_ids, failures = [], 0
    for result in results["results"][:2]:
        context.check()
        try:
            page = fetch_page(db, result["url"], search_query=query,
                              config=context.config.model_copy(update={
                                  "webpage_timeout_seconds": min(context.config.webpage_timeout_seconds, context.remaining())}),
                              before_commit=lambda: context.before_commit(db))
            context.check()
            source = db.scalar(select(WebSource).where(WebSource.canonical_url == page.source_url))
            key = (context.task_id, source.id)
            if db.get(AgentAllowedSource, key) is None:
                db.add(AgentAllowedSource(task_id=context.task_id, source_id=source.id,
                                          claim_id=context.claim["id"]))
            context.before_commit(db)
            db.commit()
            source_ids.append(str(source.id))
        except (PageError, KeyError):
            failures += 1
    return ToolResult(ok=bool(source_ids), source_ids=list(dict.fromkeys(source_ids)),
                      summary=f"Cached {len(source_ids)} public search sources; {failures} pages unavailable.",
                      error_category=None if source_ids else "no_parseable_sources")


def rag_retrieve_tool(db, context: ToolContext, query: str, top_k=5):
    context.check()
    if context.config.agent_rag_method == "dense":
        hits = search_chunks(db, query, top_k=top_k)
    else:
        hits = hybrid_retrieve(db, query, top_k=top_k,
                               use_reranker=context.config.agent_rag_method == "hybrid_rerank")
    context.check()
    evidences, chunk_ids = [], []
    for hit in hits:
        chunk = hit.chunk
        if len(chunk.text) < 12:
            continue
        content_hash = digest(chunk.text)
        if db.get(WebContent, content_hash) is None:
            # Reuse the existing immutable body store for document snapshots too.
            db.add(WebContent(content_hash=content_hash, body=chunk.text, cache_path=""))
            db.flush()
        quote = chunk.text[:800]
        identifier = uuid5(NAMESPACE_URL, f"agent:{context.claim['id']}:{context.claim['claim']}:{chunk.id}:{content_hash}")
        evidences.append(Evidence(
            evidence_id=identifier, claim_id=context.claim["id"], claim_text=context.claim["claim"],
            source_type="document", document_id=chunk.document_id, chunk_id=chunk.id,
            retrieved_at=datetime.now(timezone.utc), supporting_text=quote, relation="context",
            content_hash=content_hash, quote_start=0, quote_end=len(quote),
        ))
        chunk_ids.append(str(chunk.id))
    result = ToolResult(ok=bool(evidences), evidence_ids=[str(e.evidence_id) for e in evidences],
                      chunk_ids=chunk_ids, summary=f"Retrieved {len(evidences)} exact document excerpts.",
                      error_category=None if evidences else "no_rag_results")
    context.record_result(db, result)
    save_evidences(db, evidences, before_commit=lambda: context.before_commit(db))
    return result


def open_source_tool(db, context: ToolContext, url: str):
    context.check()
    canonical = allowed_url_syntax(url)
    source = db.scalar(select(WebSource).where(WebSource.canonical_url == canonical))
    if source is None or db.get(AgentAllowedSource, (context.task_id, source.id)) is None:
        raise ValueError("Source was not searched and cached by this task")
    content = db.get(WebContent, source.content_hash)
    if content is None or digest(content.body) != source.content_hash:
        raise ValueError("Cached source content is missing or invalid")
    # No GET is issued here: only this task's previously validated cache is opened.
    page = ParsedPage(canonical, source.fetched_url, source.title, content.body,
                      source.published_at.isoformat() if source.published_at else None,
                      source.retrieved_at.isoformat(), source.content_hash, cache_hit=True)
    claim = ClaimInput(claim_id=context.claim["id"], claim_text=context.claim["claim"])
    evidences, _ = extract_evidence(claim, page, client=context.client, max_evidences=2,
                                   timeout_seconds=context.remaining())
    result = ToolResult(ok=bool(evidences), source_ids=[str(source.id)],
                      evidence_ids=[str(e.evidence_id) for e in evidences],
                      summary=f"Validated {len(evidences)} verbatim excerpts from one task-authorized cached source.",
                      error_category=None if evidences else "no_relevant_quotes")
    context.record_result(db, result)
    save_evidences(db, evidences, before_commit=lambda: context.before_commit(db))
    return result


def verify_claim_tool(db, context: ToolContext, claim_id: str):
    if claim_id != context.claim["id"]:
        raise ValueError("Claim ID does not match the selected claim")
    from app.verification.verifier import perform_verification

    context.check()
    result = perform_verification(db, context)
    return ToolResult(ok=not result["verification_error"], summary=result["summary"][:800],
                      error_category=result["verification_error"])


class ToolRegistry:
    schemas = {"web_search_tool": WebSearchInput, "rag_retrieve_tool": RAGInput,
               "open_source_tool": OpenSourceInput, "verify_claim_tool": VerifyClaimInput}

    def __init__(self, overrides=None):
        self.functions = {"web_search_tool": web_search_tool, "rag_retrieve_tool": rag_retrieve_tool,
                          "open_source_tool": open_source_tool, "verify_claim_tool": verify_claim_tool}
        if overrides:
            if not set(overrides) <= set(self.functions):
                raise ValueError("Only registered tools may be overridden")
            self.functions.update(overrides)

    def validate(self, name, arguments):
        if name not in self.schemas:
            raise ValueError("Unknown tool")
        return self.schemas[name].model_validate(arguments)

    def call(self, name, params, db, context):
        context.check()
        result = self.functions[name](db, context, **params.model_dump())
        context.check()
        return ToolResult.model_validate(result)
