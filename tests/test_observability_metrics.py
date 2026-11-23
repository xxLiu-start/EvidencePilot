from prometheus_client import generate_latest

from app.observability.metrics import record_http_request, record_llm_trace


def test_http_metrics_use_route_templates_and_bounded_labels():
    record_http_request("GET", "/api/v1/tasks/{task_id}", 200, 0.125)
    record_http_request("GET", "unmatched/" + "x" * 200, 404, 0.01)
    output = generate_latest().decode("utf-8")
    assert 'evidencepilot_http_requests_total{method="GET",route="/api/v1/tasks/{task_id}",status_code="200"}' in output
    assert 'route="unmatched"' in output


def test_llm_metrics_record_outcome_duration_and_reported_tokens():
    record_llm_trace({
        "operation": "claim_decomposition",
        "mode": "local",
        "elapsed_ms": 1250,
        "error_category": None,
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "total_tokens": 140,
    })
    output = generate_latest().decode("utf-8")
    assert 'evidencepilot_llm_requests_total{mode="local",operation="claim_decomposition",outcome="success"}' in output
    assert 'evidencepilot_llm_tokens_total{operation="claim_decomposition",token_type="total_tokens"}' in output
