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
- Added a connector contract, explicit registry, root-scoped read-only FileConnector, WebSearchConnector adapter, and tenant-filtered read-only PostgreSQLTableConnector with explicit exposed-column allowlists.
- Routed Agent web discovery through the WebSearchConnector while retaining existing task authorization and commit fencing.
- Added OIDC JWT verification using JWKS, issuer/audience checks, asymmetric algorithm allowlisting, development-only local identity, and coarse role dependencies on write operations.
- Added unit tests for domain profiles, extensible scopes, connector registry, file path traversal, PostgreSQL tenant filtering/column allowlists, source classification, corroboration policy, OIDC token verification, role checks, task outbox dispatch, and Alembic baseline migration.
- Added a Redis/Celery task path with transactional outbox, polling dispatcher, late acknowledgements, idempotent task IDs, lease-aware retries/recovery, and outbox dead-letter status. Delivery is at-least-once.
- Added an Alembic non-destructive baseline and a Compose migration service. Production can disable startup DDL and require an applied Alembic version.
- Added a GitHub Actions workflow that compiles Python modules, validates Compose YAML, applies the Alembic baseline to PostgreSQL, and runs pytest.

## Not implemented yet

- Database-backed Tenant, User, Membership, Project and ProjectMembership models.
- Complete tenant/object-level isolation for all task, document, evidence, retrieval, feedback and evaluation APIs.
- Human-review UI, reviewer assignment, approval/rejection audit trail and signed review events.
- S3/MinIO object storage and enterprise SaaS connectors (SharePoint, Confluence, GitHub) remain pending.
- Full model gateway with provider registry, cost quotas and circuit breakers.
- OpenTelemetry, Prometheus and Grafana.
- Multi-domain manually reviewed benchmark and multi-seed evaluation.
- Queue cancellation, priority scheduling, queue backlog monitoring and full multi-worker chaos acceptance.
- Production load, chaos, backup/restore and security penetration testing.

## Readiness statement

The current increment is an architectural foundation, not a production-readiness declaration. The migration baseline is intentionally non-destructive and has no destructive downgrade; subsequent schema changes must be delivered as new Alembic revisions. The queue path uses at-least-once delivery with idempotent consumers; it does not claim exactly-once execution. OIDC protects API authentication and selected write-role boundaries, but without tenant/object-level authorization it is not safe for multiple mutually untrusted organizations. Compliance and business profiles require human review but no review workflow is implemented yet; their Claims therefore remain uncounted as supported until a future approval workflow is added.

GitHub Actions is now the verification path because the repository editing connection cannot execute code in the user's checkout. Each commit triggers the workflow; the final head SHA and run conclusion should be checked before treating the latest increment as verified.
