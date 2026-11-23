"""Low-cardinality Prometheus metrics for API and model execution."""
from prometheus_client import Counter, Gauge, Histogram


HTTP_REQUESTS = Counter(
    "evidencepilot_http_requests_total",
    "HTTP requests handled by EvidencePilot",
    ("method", "route", "status_code"),
)
HTTP_REQUEST_DURATION = Histogram(
    "evidencepilot_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
LLM_REQUESTS = Counter(
    "evidencepilot_llm_requests_total",
    "LLM requests by operation, provider mode and outcome",
    ("operation", "mode", "outcome"),
)
LLM_REQUEST_DURATION = Histogram(
    "evidencepilot_llm_request_duration_seconds",
    "LLM request duration in seconds",
    ("operation", "mode"),
)
LLM_TOKENS = Counter(
    "evidencepilot_llm_tokens_total",
    "Token usage reported by the inference provider",
    ("operation", "token_type"),
)
OUTBOX_EVENTS = Gauge(
    "evidencepilot_outbox_events",
    "Outbox events by delivery state",
    ("status",),
)


def record_http_request(method: str, route: str, status_code: int, elapsed_seconds: float) -> None:
    safe_route = route if route.startswith("/") and len(route) <= 120 else "unmatched"
    HTTP_REQUESTS.labels(method[:12], safe_route, str(status_code)).inc()
    HTTP_REQUEST_DURATION.labels(method[:12], safe_route).observe(max(0.0, elapsed_seconds))


def record_llm_trace(trace: dict) -> None:
    operation = str(trace.get("operation") or "unknown")[:64]
    mode = str(trace.get("mode") or "unknown")[:16]
    outcome = "error" if trace.get("error_category") else "success"
    LLM_REQUESTS.labels(operation, mode, outcome).inc()
    LLM_REQUEST_DURATION.labels(operation, mode).observe(max(0.0, float(trace.get("elapsed_ms") or 0) / 1000))
    for token_type in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = trace.get(token_type)
        if isinstance(value, int) and value >= 0:
            LLM_TOKENS.labels(operation, token_type).inc(value)
