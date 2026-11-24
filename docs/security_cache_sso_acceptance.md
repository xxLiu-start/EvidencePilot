# Cache isolation, real SSO and independent acceptance

## Scope of this security change

SQL cache keys now include the authenticated tenant scope. Content has a composite
primary key (cache_scope, content_hash); source URL and Claim uniqueness are
tenant-scoped. Evidence references use composite foreign keys and a PostgreSQL
check constraint binds their cache_scope to their tenant_id. Claims, content,
sources and search-history tables have ENABLE/FORCE RLS. The production role
must not be a superuser, BYPASSRLS role, table owner or a member of such a role.

Disk search, URL, content and research-replay snapshots are partitioned by
trusted tenant/project UUIDs. Scoped reads never fall back to legacy global
files. BM25 builds independent corpus statistics per tenant/project set, not
merely post-ranking filters. The scoped index cache is bounded and rebuilt on
revision changes. Global search logs no longer include plaintext search queries.

SQL cache sharing is still permitted **within the same tenant**; database RLS
is a tenant boundary. Project-specific authorization is enforced by application
context and protected evidence/document/task paths. Arbitrary SQL execution
with the runtime role remains outside this threat model: custom PostgreSQL GUCs
can be changed by arbitrary SQL. Do not grant direct DB or filesystem access to
untrusted tenants. Use parameterized queries, restricted runtime roles, and
network egress controls.

## Migration and recovery

Revision 20261124_0007 migrates the old PostgreSQL schema transactionally.
Back up the database and cache directory, stop API/Worker/Dispatcher writers,
and test the migration on a restored staging copy before deployment. Original
global cache rows remain tagged offline and are not visible to a tenant runtime
role. Only content tied to persisted tenant-owned evidence is copied. Source
metadata is reconstructed from evidence attributes; mutable global source query
lists and unattributable search histories remain offline. Previously authorized
sources without evidence must be searched again. Historical evidence UUIDs
remain unchanged. Existing old SQLite databases need a fresh schema/export
because the legacy composite-key migration is PostgreSQL-only. Do not downgrade
to global keys. Restore a pre-migration backup only in an isolated environment.

Offline benchmark scripts must use separate database/cache storage. Never
serve their unscoped offline rows from a production API.

## Real SSO acceptance

The security acceptance workflow starts a real disposable Keycloak container
and creates a confidential Streamlit client with randomly generated credentials,
then launches the actual FastAPI and Streamlit processes. Chromium follows the
OIDC authorization-code browser flow, receives the session, reads only the
signed-in user's tenant task, and logs out. HTTP checks independently validate
the actual Keycloak access token, issuer/audience, unprovisioned user denial,
ID-token rejection and DB membership rather than realm roles. No production
credentials or model API calls are required.

Run on a disposable development host with Docker, Python dependencies and
Playwright Chromium:

    python -m playwright install --with-deps chromium
    python -m scripts.security_sso_e2e

This is a real local Keycloak integration check, not a certificate for a
customer's external corporate IdP. Repeat it with the actual configured issuer,
audience, JWKS, browser redirect, TLS, token lifecycle and user provisioning.

## Independent review release gate

The security acceptance workflow publishes Bandit and pip-audit findings from
separate CI jobs. A successful automated scan is not independent human
penetration testing. Keep the security change in PR review until these gates
are recorded against the exact deployed commit:

| Gate | Evidence required | Status before execution |
| --- | --- | --- |
| PostgreSQL migration and RLS | CI logs; same URL/hash in separate tenants; denied foreign DML | Pending |
| Existing-database upgrade | Staging restore rehearsal and evidence integrity comparison | Pending |
| Keycloak browser SSO | Actual callback, tenant A/B history, logout, API audience/role checks | Pending |
| Static and dependency scans | Bandit and pip-audit artifacts; triage/fix findings | Pending |
| Independent penetration test | Separate reviewer/vendor, written scope, findings, retest and sign-off | Pending |
| Production readiness | Runtime role/grants, secrets, TLS, egress, backup/restore and monitoring | Pending |

No independent review, formal security certification, or external IdP sign-off
is implied by implementation or automated testing. A reviewer independent of
the implementation must execute the authorized staging tests and sign off on
the report. Do not test against unrelated systems or production data.
