# Tenant and Project Isolation

## Data model

The database now includes:

- `tenants`: organization boundary and optional IdP `external_id`.
- `users`: immutable identity key `(issuer, subject)`.
- `tenant_memberships`: active tenant membership and tenant role.
- `projects`: projects owned by a tenant.
- `project_memberships`: project-specific roles.
- `audit_events`: tenant/project scoped action records.

Research tasks, documents, chunks, evidence, feedback, and evaluation results carry tenant/project ownership fields. The Alembic revision `20261123_0003` backfills pre-existing rows into a `legacy-default` tenant and its `default` project. Production PostgreSQL migrations enforce non-null ownership on tenant-owned resource tables.

## Request enforcement

`get_request_context` resolves the authenticated OIDC principal to a database user and active memberships. Token role claims are deliberately not authorization grants. The database membership is authoritative.

The SQLAlchemy session applies `with_loader_criteria` tenant and project predicates to ORM SELECTs. A `before_flush` guard stamps new tenant-owned records and rejects cross-tenant or non-member project writes. Request sessions expire their identity map after scope resolution so a previously loaded ORM object cannot bypass the filters through `Session.get`.

Tenant and project roles are evaluated for the selected project, not unioned across all of a user's projects. A role held in Project A cannot authorize a write in Project B. Multi-project write operations must select a project with `X-Project-ID`.

BackgroundTasks and Celery workers first load the task, then copy its tenant/project scope into the worker session before executing the graph. The LangGraph runtime also propagates the same scope into every isolated tool Session; otherwise RAG retrieval and Evidence persistence would silently execute outside the request's tenant boundary. Evidence records are explicitly stamped with tenant/project ownership at persistence time. BM25 snapshots are shared in memory but filtered by the active tenant/project at retrieval time; dense retrieval uses the scoped ORM query.

The API uses `X-Project-ID` to narrow a request to one of the caller's memberships. Reads without that header are limited to the caller's accessible projects. New task/document/web-research writes require a selected project if the user has more than one.

## Operations

- `GET /api/v1/auth/me`: current user, tenant, roles, and accessible projects.
- `GET /api/v1/projects`: accessible projects.
- `POST /api/v1/projects`: create a project (tenant administrator).
- `POST /api/v1/projects/{project_id}/members`: add/update a project member who already belongs to the tenant.
- `GET /api/v1/audit/events`: tenant/project scoped audit history.

Provision OIDC users with `python -m scripts.provision_user`; see [OIDC Authentication](oidc_authentication.md).

## Development and migration

Local development creates a single development user, tenant, and default project. Unowned legacy/test rows are attached to that local scope only while the development identity is active. Production does not auto-provision users or tenants; apply Alembic migrations and provision users/memberships explicitly.

## PostgreSQL Row-Level Security

Alembic revision `20261123_0004` enables and forces RLS on `research_tasks`, `documents`, `document_chunks`, `evidences`, `task_feedback`, `evaluation_results`, and `audit_events`. Policies compare each row's `tenant_id` with the transaction-local PostgreSQL setting `app.tenant_id`. Missing context fails closed. The SQLAlchemy Session `after_begin` hook binds this setting from `Session.info`; request authentication sets it immediately after membership resolution, and worker messages carry the task's tenant/project/user context before the first protected query.

The application DB role must not be a PostgreSQL superuser and must not have `BYPASSRLS`. `FORCE ROW LEVEL SECURITY` subjects table owners to policies, but superusers and roles with `BYPASSRLS` still bypass them. Use a dedicated least-privilege runtime role; keep migration ownership credentials separate where possible. The migration is additive and its downgrade intentionally retains security policies.

## Security limitations

RLS currently enforces tenant boundaries on the listed tenant-owned tables; project-level authorization remains enforced by the application ORM/session layer. Global identity and membership tables are not tenant-RLS protected. The PostgreSQL integration test creates a non-owner role and verifies raw SQL cannot read another tenant's rows or see rows without a tenant setting. This is not yet a full penetration test or production security certification.

Future connectors must enforce upstream per-user/per-tenant ACLs; passing `access_context` through an interface is not enough. Add project-level database policies, full raw-SQL/write/worker/concurrency penetration tests, and end-to-end tests for every endpoint before production deployment to mutually untrusted tenants.
