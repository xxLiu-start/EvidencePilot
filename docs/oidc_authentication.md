# OIDC Authentication

EvidencePilot now has a standard OIDC JWT verification boundary in `app/auth/oidc.py`.

## Configuration

Development defaults to a local development principal so existing single-user local workflows continue to work. To enable OIDC:

```dotenv
ENVIRONMENT=development
AUTH_ENABLED=true
OIDC_ISSUER=https://identity.example.com/realms/evidencepilot
OIDC_AUDIENCE=evidencepilot-api
OIDC_JWKS_URL=https://identity.example.com/realms/evidencepilot/protocol/openid-connect/certs
```

Production startup fails if authentication is disabled or the OIDC configuration is incomplete. Production JWKS URLs must use HTTPS.

The verifier checks an allowlisted asymmetric algorithm (RS256 or ES256), signature using the provider's JWKS, issuer, audience, expiry, issued-at, and required subject claims. JWKS clients cache signing keys. Role extraction accepts a top-level `roles` array or Keycloak-style `realm_access.roles`.

Write operations have role dependencies for the RESEARCHER, REVIEWER, PROJECT_MANAGER, TENANT_ADMIN, and SUPER_ADMIN role families. Role claims must be mapped by the identity provider; a valid token without the required role receives HTTP 403.

## Important deployment boundary

This change implements authentication and coarse role gates only. It does **not** yet implement database-backed Tenant/Project membership, row-level security, or object-level ownership checks for every task, document, evidence, and search result. Therefore, do not expose an OIDC-enabled instance to multiple mutually untrusted tenants until the separate tenant-isolation work is complete.

The local development identity is not a production authentication mechanism. Production must set `ENVIRONMENT=production` and `AUTH_ENABLED=true`, then configure the real IdP and roles.
