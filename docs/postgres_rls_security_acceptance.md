# PostgreSQL RLS Production Security Checklist

This project uses PostgreSQL row-level security as defense in depth around
application-scoped tenant/project authorization. Passing automated tests is not
a substitute for a production threat assessment.

## Required deployment separation

- Run Alembic migrations as a database owner/migration role.
- Run API, Celery Worker and Outbox Dispatcher using a separate runtime role
  with NOSUPERUSER and NOBYPASSRLS. Grant only the SQL operations needed.
- Provide migration credentials via COMPOSE_DATABASE_URL and runtime credentials
  via COMPOSE_RUNTIME_DATABASE_URL, with secrets managed outside Git.
- Set ENVIRONMENT=production, AUTH_ENABLED=true, DB_AUTO_MIGRATE=false,
  TASK_QUEUE_ENABLED=true and use HTTPS OIDC issuer/JWKS endpoints.
- The API's production startup checks that RLS is ENABLED and FORCED for all
  protected tables, and refuses a SUPERUSER/BYPASSRLS runtime role.
- Keep PostgreSQL and Redis inaccessible from untrusted networks.

## Automated acceptance

GitHub Actions applies all Alembic revisions to a real PostgreSQL/pgvector
instance, then checks RLS policies and FORCE flags. Security tests exercise
non-owner SQL reads and writes, scoped Evidence and Agent state, pooled
connection reuse, concurrent transactions, API access control and background
worker task context.

These tests use ephemeral fixtures. They do not use production credentials
or real multi-company data. Repeat staging tests with the actual least-privilege
runtime role, migration role and connection pool settings.

## Important residual risks

- The app.tenant_id transaction setting is writable by arbitrary SQL.
  An attacker who can execute arbitrary SQL including set_config can spoof
  a tenant. RLS does not make arbitrary SQL execution safe.
- RLS enforces tenant isolation, not independent PostgreSQL project ACLs.
  Project permissions remain enforced by authenticated application code.
- Shared global cache tables, notably web_contents and web_sources, lack
  tenant RLS. Move to tenant-scoped content storage before serving mutually
  untrusted organizations and do not expose direct cache SQL access.
- Browser/IdP SSO integration, legally binding electronic signatures,
  independent penetration tests, backup/restore and chaos tests still need
  environment-specific acceptance.

## Release gate

Do not claim production security certification until the runtime/migration
roles, tenant-scoped shared cache, project permissions, full SSO browser flows,
independent penetration testing, backup/restore and alerting are signed off.
