# PostgreSQL RLS deployment

## 1. Apply migrations as the schema owner

Use the migration-owner connection only for deployment migrations:

```bash
DATABASE_URL="$COMPOSE_DATABASE_URL" alembic upgrade head
```

Revisions `20261123_0004` and `20261123_0005` enable and force tenant RLS on tenant-owned resource tables and task-owned execution/queue state, including `agent_runs`, `agent_steps`, `evidence_assessments`, `state_snapshots`, and `outbox_events`.

## 2. Create a separate runtime role

Connect to PostgreSQL as the schema owner and run:

```bash
psql "$COMPOSE_DATABASE_URL" -f deploy/postgres/runtime_role.sql
```

Set a strong password interactively:

```sql
\password evidencepilot_runtime
```

The role must remain `NOSUPERUSER NOBYPASSRLS`. Do not grant it table ownership, schema CREATE, BYPASSRLS, or superuser privileges.

## 3. Configure runtime services

Set `COMPOSE_RUNTIME_DATABASE_URL` to the runtime role connection string in the deployment secret store. Docker Compose uses this URL for API, Celery worker, and outbox dispatcher. The one-shot migration service continues to use `COMPOSE_DATABASE_URL` (schema owner).

The API fails production startup if the current role is a superuser, has BYPASSRLS, or required tables are not both RLS-enabled and FORCE RLS.

## 4. Operational notes

- RLS context is transaction-local and is set with `set_config('app.tenant_id', ..., true)`; it does not persist across pooled transactions.
- Request context is set only after the authenticated OIDC identity has been resolved to an active database membership.
- Background and Celery messages carry the task's tenant/project/user IDs; workers set them before the first protected query.
- RLS currently enforces tenant boundaries. Project boundaries remain enforced by application-level membership filters and write guards.
- Superusers and BYPASSRLS roles bypass PostgreSQL RLS regardless of FORCE ROW LEVEL SECURITY.
- The migration downgrade intentionally retains policies; security controls are not silently removed by application rollback.
