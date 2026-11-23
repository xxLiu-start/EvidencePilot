"""Streamlit's built-in OIDC login with IdP-issued access tokens.

Do not send the identity token to FastAPI: its audience is the UI client,
not the API. FastAPI independently verifies the access token's signature,
issuer, expiry, and audience against the configured JWKS.
"""
def access_token_from_identity(user) -> str | None:
    if not getattr(user, "is_logged_in", False):
        return None
    tokens = getattr(user, "tokens", None)
    getter = getattr(tokens, "get", None)
    if not callable(getter):
        return None
    access_token = getter("access")
    return access_token if isinstance(access_token, str) and access_token.strip() else None
