"""Authenticated, bounded internal inference transport; no local fallback."""
import hmac
import ssl
import re
from threading import Lock
from time import monotonic, sleep
from random import uniform
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
    if config.environment == "production" and any(not re.fullmatch(r"[0-9a-f]{40}", getattr(config, kind + "_revision")) for kind in ("embedding", "reranker")):
        raise ValueError("Production remote model revisions must be pinned")
    if len(config.inference_token.get_secret_value()) < 32:
        raise ValueError("Inference service token must contain at least 32 characters")

def authorized(config, authorization):
    token = config.inference_token.get_secret_value()
    return len(token) >= 32 and hmac.compare_digest(
        (authorization or "").encode("utf-8"), ("Bearer " + token).encode("utf-8"))

def _request_once(config, endpoint, payload, kind):
    validate_remote(config)
    tls = ssl.create_default_context(cafile=config.inference_ca_file or None)
    with httpx.Client(timeout=config.inference_timeout_seconds, trust_env=False, verify=tls,
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


class CircuitOpen(RuntimeError):
    pass

class Circuit:
    def __init__(self):
        self.lock = Lock()
        self.failures = 0
        self.until = 0.0
        self.probing = False

    def enter(self):
        with self.lock:
            if self.until:
                if monotonic() < self.until or self.probing:
                    raise CircuitOpen("Inference circuit open")
                self.probing = True

    def finish(self, failed):
        with self.lock:
            self.probing = False
            if failed:
                self.failures += 1
                if self.failures >= 3:
                    self.until = monotonic() + 15
            else:
                self.failures = 0
                self.until = 0.0

_circuits = {}
_circuits_lock = Lock()

def request_inference(config, endpoint, payload, kind):
    validate_remote(config)
    with _circuits_lock:
        circuit = _circuits.setdefault((config.inference_url, kind), Circuit())
    circuit.enter()
    failed = False
    try:
        for attempt in range(config.inference_max_retries + 1):
            try:
                result = _request_once(config, endpoint, payload, kind)
                failed = False
                return result
            except httpx.HTTPStatusError as exc:
                failed = exc.response.status_code in (429, 502, 503, 504)
                if not failed or attempt == config.inference_max_retries:
                    raise
            except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout):
                failed = True
                if attempt == config.inference_max_retries:
                    raise
            sleep(min(1.0, 0.25 * (2 ** attempt) + uniform(0, 0.1)))
        raise RuntimeError("Inference retry budget exhausted")
    except (httpx.ReadTimeout, httpx.ReadError):
        failed = True
        raise
    finally:
        circuit.finish(failed)
