import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, Integer, BigInteger, Float, Boolean, ForeignKey, UniqueConstraint, JSON, event
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, Session
from app.db.session import Base
from app.retrieval.embedding import BGE_M3_DIMENSIONS

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="VIEWER")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class ProjectMembership(Base):
    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="VIEWER")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class ResearchTask(Base):
    __tablename__ = "research_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="general_research")
    query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        default="queued"
    )
    budget: Mapped[int] = mapped_column(
        Integer,
        default=6
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow
    )


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    # Legacy chunks remain readable until the explicit backfill has run.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(BGE_M3_DIMENSIONS).with_variant(JSON(none_as_null=True), "sqlite"),
        nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def embedding_dimensions(self) -> int | None:
        return len(self.embedding) if self.embedding is not None else None


class RetrievalIndexRevision(Base):
    __tablename__ = "retrieval_index_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


def bump_index_revision(connection) -> None:
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Retrieval index revisions require PostgreSQL or SQLite")
    statement = insert(RetrievalIndexRevision).values(id=1, revision=1)
    connection.execute(statement.on_conflict_do_update(
        index_elements=["id"], set_={"revision": RetrievalIndexRevision.revision + 1}
    ))


@event.listens_for(Session, "after_flush")
def _track_chunk_changes(session, flush_context) -> None:
    changed = any(isinstance(obj, DocumentChunk) for obj in session.new | session.deleted)
    changed = changed or any(
        isinstance(obj, DocumentChunk) and session.is_modified(obj, include_collections=False)
        for obj in session.dirty
    )
    if changed:
        bump_index_revision(session.connection())


class ClaimRecord(Base):
    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("claim_id", "claim_text"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)


class WebContent(Base):
    __tablename__ = "web_contents"
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    cache_path: Mapped[str] = mapped_column(Text, nullable=False)


class WebSource(Base):
    __tablename__ = "web_sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    fetched_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(ForeignKey("web_contents.content_hash"), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    search_queries: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class WebSearchRecord(Base):
    __tablename__ = "web_searches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"), nullable=False, index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    elapsed_ms: Mapped[float] = mapped_column(Float, nullable=False)
    cache_hit: Mapped[int] = mapped_column(Integer, nullable=False)
    source_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False)


class EvidenceRecord(Base):
    __tablename__ = "evidences"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    claim_record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_url: Mapped[str | None] = mapped_column(ForeignKey("web_sources.canonical_url"), nullable=True)
    content_hash: Mapped[str] = mapped_column(ForeignKey("web_contents.content_hash"), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("document_chunks.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supporting_text: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("task_id", "step_index"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False)
    observation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    elapsed_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentAllowedSource(Base):
    __tablename__ = "agent_allowed_sources"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("web_sources.id"), primary_key=True)
    claim_id: Mapped[str] = mapped_column(Text, nullable=False)


class EvidenceAssessment(Base):
    __tablename__ = "evidence_assessments"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentPolicyDecision(Base):
    __tablename__ = "agent_policy_decisions"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), primary_key=True)
    decision_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposed_action: Mapped[str] = mapped_column(String(30), nullable=False)
    selected_action: Mapped[str] = mapped_column(String(30), nullable=False)
    target_claim_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StateSnapshot(Base):
    __tablename__ = "state_snapshots"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_node: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolExecutionReceipt(Base):
    __tablename__ = "tool_execution_receipts"
    step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_steps.id"), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentReflection(Base):
    __tablename__ = "agent_reflections"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), primary_key=True)
    rewrite_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_category: Mapped[str] = mapped_column(String(80), nullable=False)
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
    original_query: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskFeedback(Base):
    __tablename__ = "task_feedback"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("research_tasks.id"), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"), nullable=True, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)



class OutboxEvent(Base):
    """Transactional event used to hand durable task creation to the queue dispatcher."""

    __tablename__ = "outbox_events"
    __table_args__ = (UniqueConstraint("event_type", "aggregate_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("research_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
