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

BackgroundTasks and Celery workers first load the task, then copy its tenant/project scope into the worker session before executing the graph. BM25 snapshots are shared in memory but filtered by the active tenant/project at retrieval time; dense retrieval uses the scoped ORM query.

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

## Security limitations

This is application-level ORM isolation, not PostgreSQL Row-Level Security. Every new endpoint must use the request-scoped session and must not issue unscoped raw SQL against tenant data. New background workers must set task-derived scope before querying related records. Global immutable web-source caches may be shared, but APIs must only reveal them through tenant-scoped evidence records.

Future connectors must enforce upstream per-user/per-tenant ACLs; passing `access_context` through an interface is not enough. Add PostgreSQL RLS, cross-tenant penetration tests, and end-to-end tests for every endpoint before production deployment to mutually untrusted tenants.
