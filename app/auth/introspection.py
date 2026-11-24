"""RFC 7662 live introspection after local JWT verification; no positive cache."""
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException

def validate_introspection(config):
    url = config.oidc_introspection_url
    if not url and config.environment != "production":
        return
    parsed = urlsplit(url)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.query
            or (config.environment == "production" and parsed.scheme != "https")
            or not config.oidc_introspection_client_id
            or not config.oidc_introspection_client_secret.get_secret_value()):
        raise RuntimeError("Valid OIDC introspection endpoint and confidential client required")

def assert_active(config, token, claims):
    validate_introspection(config)
    if not config.oidc_introspection_url:
        return
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = client.post(config.oidc_introspection_url,
                auth=(config.oidc_introspection_client_id, config.oidc_introspection_client_secret.get_secret_value()),
                data={"token": token, "token_type_hint": "access_token"})
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("active"), bool):
            raise ValueError("Invalid introspection response")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Identity provider temporarily unavailable") from None
    if (data["active"] is not True or data.get("sub") != claims["sub"]
            or ("iss" in data and data["iss"] != claims["iss"])):
        raise HTTPException(401, "Inactive access token", headers={"WWW-Authenticate": "Bearer"})
