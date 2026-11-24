"""Authenticated, bounded internal inference transport; no local fallback."""
import hmac
from urllib.parse import urlsplit

import httpx

def validate_remote(config):
    url = urlsplit(config.inference_url)
    if (url.scheme not in {"http", "https"} or not url.hostname
            or url.username or url.password or url.query or url.fragment
            or url.path not in {"", "/"}):
        raise ValueError("Invalid inference service URL")
    if config.environment == "production" and url.scheme != "https":
        raise ValueError("Production inference requires HTTPS")
    if len(config.inference_token.get_secret_value()) < 32:
        raise ValueError("Inference service token must contain at least 32 characters")

def authorized(config, authorization):
    token = config.inference_token.get_secret_value()
    return len(token) >= 32 and hmac.compare_digest(
        (authorization or "").encode("utf-8"), ("Bearer " + token).encode("utf-8"))

def request_inference(config, endpoint, payload, kind):
    validate_remote(config)
    with httpx.Client(timeout=config.inference_timeout_seconds, trust_env=False,
                      follow_redirects=False) as client:
        with client.stream("POST", config.inference_url.rstrip("/") + endpoint,
                           json=payload, headers={"Authorization": "Bearer " + config.inference_token.get_secret_value()}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 4_000_000:
                    raise ValueError("Inference response exceeds size limit")
    import json
    result = json.loads(data)
    if result.get("model") != getattr(config, kind + "_model") or result.get("revision") != getattr(config, kind + "_revision"):
        raise ValueError("Inference model identity mismatch")
    return result["values"]
