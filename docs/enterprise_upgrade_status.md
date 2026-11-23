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
- Added OIDC JWT verification using JWKS, issuer/audience checks, and asymmetric algorithm allowlisting.
- Added database-backed User/Tenant/TenantMembership/Project/ProjectMembership models, tenant/project scoped ORM query filters, cross-scope write guards, project-specific role evaluation, worker and isolated-tool-session scope propagation, explicit Evidence ownership stamping, an OIDC user provisioning CLI, and PostgreSQL RLS policies on tenant-owned resource tables.
- Added project discovery/administration APIs, Streamlit project selection and `X-Project-ID` transport, and tenant/project scoped audit event APIs.
- Added an optional localhost-only Keycloak development realm and Compose identity profile for API integration testing.
- Added unit tests for domain profiles, extensible scopes, connector registry, file path traversal, PostgreSQL tenant filtering/column allowlists, source classification, corroboration policy, OIDC token verification, role checks, task outbox dispatch, and Alembic baseline migration.
- Added a Redis/Celery task path with transactional outbox, polling dispatcher, late acknowledgements, idempotent task IDs, lease-aware retries/recovery, and outbox dead-letter status. Delivery is at-least-once.
- Added an Alembic non-destructive baseline and a Compose migration service. Production can disable startup DDL and require an applied Alembic version.
- Added basic Prometheus HTTP/LLM metrics, an internal `/metrics` endpoint, and Compose Prometheus/Grafana services.
- Added a GitHub Actions workflow that compiles Python modules, validates Compose YAML, applies the Alembic baseline to PostgreSQL, and runs pytest.

## Not implemented yet

- Reviewer assignment queues, dual-control quorum, legally binding signatures, and revocation/escalation; a basic immutable approval/rejection API, audit events and Streamlit Reviewer page are implemented.
- Real IdP/browser SSO acceptance, token lifecycle/refresh and protected production deployment of Streamlit secrets; opt-in Streamlit OIDC login/callback/logout and IdP access-token forwarding are implemented.
- PostgreSQL RLS is enabled and forced for tenant-owned resource tables and task-owned Agent/queue state, with transaction-local tenant context, worker context propagation, raw-SQL read/write integration tests, and production startup checks rejecting superuser/BYPASSRLS runtime roles. Full penetration testing and project-level database policies remain pending.
- S3/MinIO object storage and enterprise SaaS connectors (SharePoint, Confluence, GitHub) remain pending.
- Full model gateway with provider registry, cost quotas and circuit breakers.
- OpenTelemetry distributed tracing, alert rules, log aggregation and queue/worker SLO dashboards; basic Prometheus metrics and Grafana datasource provisioning are implemented.
- Multi-domain manually reviewed benchmark and multi-seed evaluation.
- Queue cancellation, priority scheduling, queue backlog monitoring and full multi-worker chaos acceptance.
- Production load, chaos, backup/restore and security penetration testing.

## Readiness statement

The current increment is an architectural foundation, not a production-readiness declaration. PostgreSQL RLS is enabled and forced on tenant-owned resource tables using transaction-local `app.tenant_id`; the runtime database role must not be a superuser or have `BYPASSRLS`. Project-level RLS and full penetration testing are still required. The migration baseline is intentionally non-destructive and has no destructive downgrade; subsequent schema changes must be delivered as new Alembic revisions. The queue path uses at-least-once delivery with idempotent consumers; it does not claim exactly-once execution. OIDC identity is resolved to database-backed tenant/project memberships and ORM queries/writes are scoped accordingly. PostgreSQL RLS exists for tenant and task-linked tables, and CI exercises a non-owner runtime role, but global shared-cache rows and independent penetration tests remain unresolved; this is not production security certification for mutually untrusted organizations. Compliance and business Claims requiring review now have immutable approval/rejection events. Coverage is recomputed from matching-fingerprint approvals after semantic verification, while historical reports/checkpoints are preserved. Further reviewer lifecycle controls remain pending.

GitHub Actions is the verification path because the repository editing connection cannot execute code in the user's checkout. The tenant/project enforcement and isolated-tool-session scope propagation passed the full workflow at commit `7ad09aadcb8cd7fa9aa32c1b6d8411d7dc9ee93a` (214+ tests, including tenant/project isolation and the end-to-end API → Agent → RAG → Evidence flow). Subsequent commits must be checked independently.
