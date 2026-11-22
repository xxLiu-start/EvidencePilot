# vLLM production metrics

- **Resource ID:** 14
- **Publisher:** vLLM Documentation
- **Official source:** https://docs.vllm.ai/en/stable/usage/metrics/
- **Purpose:** Design production monitoring, capacity planning and incident diagnosis.

## Reading notes

vLLM exposes Prometheus-style metrics through /metrics. Combine engine metrics with application success, timeout, cost, model version and user-feedback metrics for end-to-end observability.

## Enterprise selection questions

- Are TTFT, TPOT, throughput, queue depth and KV-cache utilization monitored
- Can metrics be segmented by model and tenant
- Do alerts map to the SLA
- Is load testing documented?
