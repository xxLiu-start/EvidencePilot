# Enterprise SSO via Streamlit OpenID Connect

Streamlit >=1.64 provides built-in OIDC authorization-code sign-in, a login
callback at /oauth2callback, browser identity cookie handling, and (when
explicitly enabled) access-token exposure. EvidencePilot uses these primitives
rather than implementing a custom OAuth callback or storing tokens in query
parameters.

## Configuration

1. Register a **confidential OIDC web client** with the IdP. For local use,
   set the redirect URI to http://localhost:8501/oauth2callback. For deployment
   use the exact HTTPS public callback URI.
2. Ensure that the IdP issues an access token with the API audience
   `evidencepilot-api`, an issuer matching FastAPI's `OIDC_ISSUER`, and a
   signing key discoverable through `OIDC_JWKS_URL`.
3. Copy `deploy/streamlit-secrets.toml.example` to a locally available
   `.streamlit/secrets.toml`. Replace placeholders with your confidential
   `client_secret` and a random cookie secret. The actual secrets file is
   excluded by .gitignore. Provide it to containers through your orchestrator's
   secrets facility, never by baking it into an image.
4. Set `STREAMLIT_OIDC_ENABLED=true` on the Streamlit process and configure
   FastAPI `AUTH_ENABLED=true`, `OIDC_ISSUER`, `OIDC_AUDIENCE` and
   `OIDC_JWKS_URL`. Provision a matching `(iss, sub)` user and memberships
   using `scripts.provision_user`.

In a local development shell:

```bash
export STREAMLIT_OIDC_ENABLED=true
streamlit run frontend/streamlit_app.py
```

The `identity` Compose profile includes a development Keycloak realm, but its
import does **not** provision a production-ready confidential Streamlit client.
Create that OIDC client in the Keycloak admin UI and set its confidential
client secret in the local Streamlit secrets file.

## Security model

`st.login()` handles the browser authorization redirect, state/nonce and
callback. The frontend reads **only** `st.user.tokens["access"]` (when
configured via `expose_tokens=["access"]`) and attaches it to API requests as
`Authorization: Bearer ...`. The ID token is never accepted as a substitute
for an API access token. FastAPI independently validates the JWT using JWKS,
issuer, audience, signature and expiry before resolving database-backed
memberships.

`st.logout()` clears the Streamlit identity cookie. Token refresh is not
implemented: when an access token expires the API refuses it and the user must
reauthenticate. Do not log tokens, persist them in query strings or expose
them in the Streamlit UI.

## Acceptance remaining

An automated unit test covers token selection and no ID-token fallback. A
real browser/IdP acceptance run is required for authorization redirect,
callback, role provisioning, audience validation, login expiry, logout and
multi-user session isolation before claiming SSO production acceptance.
