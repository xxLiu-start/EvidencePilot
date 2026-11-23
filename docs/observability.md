# Observability

EvidencePilot now exports basic Prometheus metrics.

## Metrics

- `evidencepilot_http_requests_total`: HTTP requests by method, route template, and status code.
- `evidencepilot_http_request_duration_seconds`: HTTP latency histogram by method and route template.
- `evidencepilot_llm_requests_total`: LLM requests by operation, mode, and success/error outcome.
- `evidencepilot_llm_request_duration_seconds`: LLM latency histogram.
- `evidencepilot_llm_tokens_total`: provider-reported prompt, completion, and total tokens.

Route labels use FastAPI route templates rather than concrete paths to avoid task-ID cardinality. Token metrics are emitted only when the provider returns usage data.

The `/metrics` endpoint can be disabled with `METRICS_ENABLED=false`. It is intended for internal scraping; do not expose it publicly through an internet-facing gateway. Compose binds Prometheus and Grafana ports to localhost and Prometheus scrapes the API over the internal Compose network.

## Docker Compose

With the `apps` profile, Compose includes:

- Prometheus at `http://127.0.0.1:9090`
- Grafana at `http://127.0.0.1:3000`

Grafana is provisioned with a Prometheus datasource. Change `GRAFANA_ADMIN_PASSWORD` before any shared or production deployment.

## Current limitations

This increment provides request and LLM metrics, but not full OpenTelemetry distributed traces, queue backlog dashboards, alert rules, log aggregation, or SLO-based alerting. Those remain follow-up work.
