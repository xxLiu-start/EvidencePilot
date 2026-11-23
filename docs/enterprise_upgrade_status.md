# Enterprise Upgrade Status

This document records what is implemented in the current upgrade increment and what remains intentionally incomplete.

## Implemented in this increment

- Added seven validated domain profiles, including general, technology, market, academic, compliance, business due diligence, and model-infrastructure research.
- Added `domain_id` to research task creation and persistence, with an additive compatibility migration for existing databases.
- Added domain discovery APIs.
- Passed domain guidance to Planner and Router.
- Applied profile maximum Claim count, freshness window, minimum independent-source count, and fail-closed human-review requirement to new Claims.
- Generalized EvidenceScope to accept domain-specific attributes while retaining legacy model scope fields.
- Added a generic source-authority registry for government, standards, academic, known official documentation, and unknown web sources. Classification is a hint, not a trust guarantee.
- Added a connector contract, explicit registry, root-scoped read-only FileConnector, and WebSearchConnector adapter.
- Routed Agent web discovery through the WebSearchConnector while retaining existing task authorization and commit fencing.
- Added OIDC JWT verification using JWKS, issuer/audience checks, asymmetric algorithm allowlisting, development-only local identity, and coarse role dependencies on write operations.
- Added unit tests for domain profiles, extensible scopes, connector registry, file path traversal, source classification, corroboration policy, OIDC token verification, and role checks.

## Not implemented yet

- Database-backed Tenant, User, Membership, Project and ProjectMembership models.
- Complete tenant/object-level isolation for all task, document, evidence, retrieval, feedback and evaluation APIs.
- Human-review UI, reviewer assignment, approval/rejection audit trail and signed review events.
- Queue foundation now includes Redis/Celery, a transactional outbox, a polling dispatcher, late acknowledgements, idempotent task IDs, lease-aware retries/recovery, and outbox dead-letter status. Cancellation, priorities, queue monitoring, and full multi-worker chaos acceptance remain pending.
- Alembic migration history; current additive schema initialization remains.
- S3/MinIO object storage and enterprise SaaS connectors.
- Full model gateway with provider registry, cost quotas and circuit breakers.
- OpenTelemetry, Prometheus and Grafana.
- Multi-domain manually reviewed benchmark and multi-seed evaluation.
- Production load, chaos, backup/restore and security penetration testing.

## Readiness statement

The current increment is an architectural foundation, not a production-readiness declaration. The queue path uses at-least-once delivery with idempotent consumers; it does not claim exactly-once execution. OIDC protects API authentication and selected write-role boundaries, but without tenant/object-level authorization it is not safe for multiple mutually untrusted organizations. Compliance and business profiles require human review but no review workflow is implemented yet; their Claims therefore remain uncounted as supported until a future approval workflow is added.

The repository's existing tests have not been re-run by the GitHub file-editing connection. The new tests are committed and must pass in CI or a local development environment before this increment is considered verified.
