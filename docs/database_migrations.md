# Database Migrations

EvidencePilot now includes an Alembic baseline and additive revisions:

- `20261123_0001_baseline.py`: initial non-destructive schema baseline.
- `20261123_0002_outbox_retry_schedule.py`: bounded outbox retry schedule.
- `20261123_0003_tenant_project_isolation.py`: tenant/project membership tables and ownership backfill for legacy rows.

## Development

The local development default is `DB_AUTO_MIGRATE=true`; the API retains its existing additive schema bootstrap for convenience.

## Deployment

Run migrations as a deployment step before starting the API:

```bash
DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DB' alembic upgrade head
```

For Docker Compose, the `migration` service runs `alembic upgrade head` and the API waits for that one-shot service to complete. Compose sets `DB_AUTO_MIGRATE=false` by default so the API does not perform DDL at startup.

For production, set:

```dotenv
ENVIRONMENT=production
DB_AUTO_MIGRATE=false
```

Production API startup rejects `DB_AUTO_MIGRATE=true` and requires an `alembic_version` row. This separates schema changes from application startup.

## Baseline behavior and limitations

The initial revision creates missing tables and applies additive compatibility columns for existing installations. It does not drop existing tables or data. Its downgrade is intentionally a no-op because it may be adopted by databases containing user data.

The baseline is non-destructive, and ownership migration backfills existing resources into a `legacy-default` tenant and `default` project. The migration does not drop existing data. This is still an early migration history; future schema changes must be added as new revisions. All future schema changes must be added as new Alembic revisions with explicit upgrade logic and data-safe downgrade behavior where possible. Large-table changes should use expand/migrate/contract patterns.
