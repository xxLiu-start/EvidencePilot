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
- Added database-backed User/Tenant/TenantMembership/Project/ProjectMembership models, tenant/project scoped ORM query filters, cross-scope write guards, worker scope propagation, and an OIDC user provisioning CLI.
- Added project discovery/administration APIs, Streamlit project selection and `X-Project-ID` transport, and tenant/project scoped audit event APIs.
- Added an optional localhost-only Keycloak development realm and Compose identity profile for API integration testing.
- Added unit tests for domain profiles, extensible scopes, connector registry, file path traversal, PostgreSQL tenant filtering/column allowlists, source classification, corroboration policy, OIDC token verification, role checks, task outbox dispatch, and Alembic baseline migration.
- Added a Redis/Celery task path with transactional outbox, polling dispatcher, late acknowledgements, idempotent task IDs, lease-aware retries/recovery, and outbox dead-letter status. Delivery is at-least-once.
- Added an Alembic non-destructive baseline and a Compose migration service. Production can disable startup DDL and require an applied Alembic version.
- Added basic Prometheus HTTP/LLM metrics, an internal `/metrics` endpoint, and Compose Prometheus/Grafana services.
- Added a GitHub Actions workflow that compiles Python modules, validates Compose YAML, applies the Alembic baseline to PostgreSQL, and runs pytest.

## Not implemented yet

- Human-review UI, reviewer assignment, approval/rejection audit trail and signed review events.
- Streamlit OIDC sign-in/callback flow to obtain and populate the per-session access token; bearer forwarding and project selection are implemented, but token acquisition is not.
- PostgreSQL Row-Level Security and cross-tenant penetration tests; current isolation is enforced at the application/ORM layer.
- S3/MinIO object storage and enterprise SaaS connectors (SharePoint, Confluence, GitHub) remain pending.
- Full model gateway with provider registry, cost quotas and circuit breakers.
- OpenTelemetry distributed tracing, alert rules, log aggregation and queue/worker SLO dashboards; basic Prometheus metrics and Grafana datasource provisioning are implemented.
- Multi-domain manually reviewed benchmark and multi-seed evaluation.
- Queue cancellation, priority scheduling, queue backlog monitoring and full multi-worker chaos acceptance.
- Production load, chaos, backup/restore and security penetration testing.

## Readiness statement

The current increment is an architectural foundation, not a production-readiness declaration. The migration baseline is intentionally non-destructive and has no destructive downgrade; subsequent schema changes must be delivered as new Alembic revisions. The queue path uses at-least-once delivery with idempotent consumers; it does not claim exactly-once execution. OIDC identity is resolved to database-backed tenant/project memberships and ORM queries/writes are scoped accordingly. However, PostgreSQL RLS and full cross-tenant penetration tests are still missing, so this is not yet a production security certification for mutually untrusted organizations. Compliance and business profiles require human review but no review workflow is implemented yet; their Claims therefore remain uncounted as supported until a future approval workflow is added.

GitHub Actions is now the verification path because the repository editing connection cannot execute code in the user's checkout. Each commit triggers the workflow; the final head SHA and run conclusion should be checked before treating the latest increment as verified.
