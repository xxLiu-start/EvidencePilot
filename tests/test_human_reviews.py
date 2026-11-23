"""End-to-end reviewer authorization, immutable decisions and Claim transition."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth.oidc import Principal, get_current_principal
from app.db.models import (
    AgentRun, AuditEvent, ClaimRecord, EvidenceAssessment, EvidenceRecord,
    HumanReview, Project, ProjectMembership, ResearchTask, Tenant,
    TenantMembership, User, WebContent, WebSource,
)
from app.db.session import Base, get_db
from app.main import app
from app.search.cache import digest
from app.verification.verifier import assess_claim


def test_human_review_approval_requires_membership_and_current_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reviews.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        tenant = Tenant(slug="review-tenant", name="Review tenant", external_id="review-tenant")
        other_tenant = Tenant(slug="other-tenant", name="Other tenant", external_id="other-tenant")
        author = User(issuer="https://id.example", subject="author")
        reviewer = User(issuer="https://id.example", subject="reviewer")
        outsider = User(issuer="https://id.example", subject="outsider")
        db.add_all([tenant, other_tenant, author, reviewer, outsider])
        db.flush()
        project = Project(tenant_id=tenant.id, slug="review", name="Review project")
        other_project = Project(tenant_id=other_tenant.id, slug="other", name="Other project")
        db.add_all([project, other_project])
        db.flush()
        db.add_all([
            TenantMembership(tenant_id=tenant.id, user_id=author.id, role="RESEARCHER"),
            TenantMembership(tenant_id=tenant.id, user_id=reviewer.id, role="VIEWER"),
            TenantMembership(tenant_id=other_tenant.id, user_id=outsider.id, role="REVIEWER"),
            ProjectMembership(project_id=project.id, user_id=author.id, role="RESEARCHER"),
            ProjectMembership(project_id=project.id, user_id=reviewer.id, role="REVIEWER"),
            ProjectMembership(project_id=other_project.id, user_id=outsider.id, role="REVIEWER"),
        ])
        task = ResearchTask(query="Check verified claim", status="partial", budget=2,
                            tenant_id=tenant.id, project_id=project.id,
                            created_by_user_id=author.id)
        claim_text = "The service supports private deployment."
        claim_record = ClaimRecord(claim_id="C1", claim_text=claim_text)
        db.add_all([task, claim_record])
        db.flush()
        body = claim_text
        hash_value = digest(body)
        db.add(WebContent(content_hash=hash_value, body=body, cache_path=""))
        source_url = "https://example.org/review-evidence"
        db.add(WebSource(canonical_url=source_url, fetched_url=source_url,
                         title="Primary evidence", content_hash=hash_value,
                         retrieved_at=now, published_at=now,
                         source_type="web", search_queries=[]))
        evidence = EvidenceRecord(id=uuid4(), claim_record_id=claim_record.id,
                                  tenant_id=tenant.id, project_id=project.id,
                                  source_type="web", source_url=source_url,
                                  content_hash=hash_value, retrieved_at=now,
                                  published_at=now, supporting_text=body,
                                  relation="supports", scope={}, quote_start=0,
                                  quote_end=len(body))
        db.add(evidence)
        claim = {"id": "C1", "claim": claim_text, "required_source": "any",
                 "priority": "high", "human_review_required": True,
                 "min_independent_sources": 1, "max_evidence_age_days": 365}
        state = {"status": "partial", "claims": [claim],
                 "claim_statuses": {"C1": {"evidence_ids": [str(evidence.id)]}},
                 "coverage": 0, "final_report": "Research completed, awaiting human approval."}
        db.add(AgentRun(task_id=task.id, state=state, updated_at=now))
        db.flush()
        fingerprint = assess_claim(db, claim, [str(evidence.id)], task_id=task.id)["fingerprint"]
        db.add(EvidenceAssessment(task_id=task.id, claim_id="C1",
                                  fingerprint=fingerprint, created_at=now,
                                  result={"verified": True, "verdict": "SUPPORTED",
                                          "supporting_evidence_ids": [str(evidence.id)],
                                          "reason": "Evidence supports the claim"}))
        db.commit()
        task_id, project_id, reviewer_id = task.id, project.id, reviewer.id

    active = {"subject": "outsider", "tenant": "other-tenant"}

    def principal():
        return Principal(subject=active["subject"], issuer="https://id.example",
                         audience=("evidencepilot-api",), tenant_id=active["tenant"])

    def get_session():
        with Session(engine, autoflush=False) as db:
            yield db

    app.dependency_overrides[get_current_principal] = principal
    app.dependency_overrides[get_db] = get_session
    client = TestClient(app)
    route = f"/api/v1/tasks/{task_id}/claims/C1/reviews"
    payload = {"fingerprint": fingerprint, "decision": "approved",
               "comment": "Independently verified against the original source"}
    try:
        assert client.post(route, json=payload).status_code == 404
        active.update(subject="author", tenant="review-tenant")
        assert client.post(route, json=payload).status_code == 403
        active.update(subject="reviewer", tenant="review-tenant")
        pending = client.get(f"/api/v1/tasks/{task_id}/report")
        assert pending.status_code == 200
        assert pending.json()["claim_statuses"]["C1"]["status"] == "INSUFFICIENT"

        response = client.post(route, json=payload, headers={"X-Project-ID": str(project_id)})
        assert response.status_code == 201, response.text
        assert client.post(route, json=payload).status_code == 409
        approved = client.get(f"/api/v1/tasks/{task_id}/report")
        assert approved.status_code == 200, approved.text
        assert approved.json()["claim_statuses"]["C1"]["status"] == "SUPPORTED"
        assert approved.json()["coverage"] == 1.0

        with Session(engine) as db:
            reviews = list(db.scalars(select(HumanReview)))
            assert len(reviews) == 1 and reviews[0].reviewer_user_id == reviewer_id
            assert db.scalar(select(AuditEvent).where(
                AuditEvent.action == "human_review.approved"
            )) is not None
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()
