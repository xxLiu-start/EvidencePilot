# Human Review Workflow

EvidencePilot now provides immutable, tenant-scoped human review decisions for
Claims whose domain profile requires an independent reviewer.

Endpoints:

- `GET /api/v1/tasks/{task_id}/claims/{claim_id}/reviews` lists decisions
  scoped to the authenticated caller's tenant/project.
- `POST /api/v1/tasks/{task_id}/claims/{claim_id}/reviews` accepts a 64-char
  evidence fingerprint, `approved` or `rejected`, and a non-empty reason.

Only a database-provisioned `REVIEWER`, `PROJECT_MANAGER`,
`TENANT_ADMIN` or `SUPER_ADMIN` can submit a decision. Research authors
cannot approve/reject their own task. If the user belongs to multiple projects
the request must select the task's project using `X-Project-ID`.

Approval requires a matching, current EvidenceAssessment with a valid semantic
`SUPPORTED` verdict, fresh evidence, source requirements met, and no
confirmed conflict. Decisions are immutable: each reviewer can decide at most
once for a given Claim/fingerprint. An explicit rejection takes precedence over
approvals for the same fingerprint. Any changed evidence fingerprint requires
a fresh independent decision. The reviewer action and fingerprint are written
to the tenant-scoped audit log.

The `Human Review` Streamlit page provides a reviewer form and decision
history. `GET /api/v1/tasks/{task_id}/report` dynamically recomputes the
Claim's effective status and coverage after human review, without silently
re-running the Agent or rewriting historical checkpoints. The saved original
report text and task's terminal status are historical artifacts and are not
automatically rewritten to a new status.

## Remaining limitations

This is a basic independent review workflow, not a regulated electronic-signature
product. No legally binding signatures, dual-control quorum, reassignment,
SLA/escalations, or administrative revocation process are provided. Reviewer
membership is resolved at submission time but not automatically revalidated
for previous historical decisions. External compliance sign-off, audit export,
and production penetration testing remain required.
