# OIDC Authentication and Authorization

EvidencePilot validates OIDC access tokens and then resolves the identity against persisted database memberships. A valid token alone does not grant access to a tenant or project.

## Configuration

```dotenv
ENVIRONMENT=production
AUTH_ENABLED=true
OIDC_ISSUER=https://identity.example.com/realms/evidencepilot
OIDC_AUDIENCE=evidencepilot-api
OIDC_JWKS_URL=https://identity.example.com/realms/evidencepilot/protocol/openid-connect/certs
DB_AUTO_MIGRATE=false
```

The verifier allows RS256 and ES256, checks the JWKS signature, issuer, audience, expiry, issued-at, subject, and required claims. Production issuer and JWKS endpoints must use HTTPS.

## Database-backed membership is authoritative

The API resolves the `(issuer, subject)` pair to a provisioned `users` row, then checks:

1. An active `tenant_memberships` row.
2. An active tenant.
3. An active project membership, unless the tenant membership role is `TENANT_ADMIN` or `SUPER_ADMIN`.

OIDC role claims are not used as authorization grants. Tenant and project roles are read from the database. A user who is not provisioned, has no active tenant membership, or has no project membership receives HTTP 403.

When a token contains `tenant_id`, it must match a tenant's configured `external_id` and the user must have a membership in that tenant. Without that claim, the user must belong to exactly one active tenant. Multi-tenant users select a tenant through an IdP-issued tenant claim; the API does not trust an arbitrary request parameter for tenant selection.

Every API request receives tenant/project filters at the SQLAlchemy session boundary. The `X-Project-ID` header narrows access to one project. Without it, reads are limited to all projects the user belongs to; create operations require an explicit project when the user has more than one project. Cross-tenant and non-member project writes are rejected.

## Provision a user

After the user has been created in the IdP, provision the exact issuer and `sub` claim into EvidencePilot. Do not use email as the identity key.

```bash
python -m scripts.provision_user \
  --tenant acme \
  --tenant-name "Acme Research" \
  --external-tenant-id acme \
  --issuer https://identity.example.com/realms/acme \
  --subject '<exact-sub-claim>' \
  --email researcher@acme.example \
  --tenant-role RESEARCHER \
  --project default \
  --project-role RESEARCHER
```

Tenant administrators can create projects with `POST /api/v1/projects` and assign existing tenant members with `POST /api/v1/projects/{project_id}/members`. The `GET /api/v1/auth/me` endpoint returns the current user's tenant, roles, and accessible projects. Project administration is also available through the API, not by trusting role claims in the token.

Audit events are available to tenant/project administrators at `GET /api/v1/audit/events`.

## Streamlit frontend

Streamlit now supports opt-in built-in OIDC browser login and logout (`STREAMLIT_OIDC_ENABLED=true`). The frontend uses the IdP-issued **access token** from `st.user.tokens` rather than forwarding an ID token, and FastAPI independently validates JWT signature, issuer, audience and expiry. Configure a confidential IdP client and `expose_tokens=["access"]` in `.streamlit/secrets.toml`; see [Streamlit SSO](streamlit_sso.md). Actual IdP/browser integration acceptance is still required before production use.

## Local Keycloak development provider

A localhost-only development realm can be started with:

```bash
docker compose --profile identity up -d keycloak
```

For a host-run API, set the issuer and JWKS URLs to the local Keycloak realm. Obtain a development token with the imported CLI client, then provision its exact `sub` using `scripts/provision_user.py`. The imported account and password grant are for local smoke tests only. Rotate placeholder credentials and never expose the Keycloak development service publicly or reuse this realm in production.

## Remaining security boundary

The implementation combines application-level tenant/project isolation with PostgreSQL RLS on tenant-owned resource tables. Each PostgreSQL transaction receives a transaction-local `app.tenant_id`; requests set it after membership resolution, while background and Celery jobs receive tenant context in their task payload. RLS does not replace upstream ACL enforcement in future SharePoint/Confluence/GitHub connectors. Production must use a least-privilege database role without superuser or `BYPASSRLS`; full penetration testing, project-level RLS, and a complete service-path audit remain required. Do not treat an arbitrary custom connector's `access_context` parameter as proof of upstream authorization.
