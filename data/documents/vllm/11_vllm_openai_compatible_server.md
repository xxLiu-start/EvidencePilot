# vLLM OpenAI-compatible server

- **Resource ID:** 11
- **Publisher:** vLLM Documentation
- **Official source:** https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/
- **Purpose:** Assess self-hosted serving interfaces and API compatibility.

## Reading notes

vLLM exposes HTTP endpoints compatible with several APIs, including OpenAI-style endpoints and Anthropic Messages. The documentation warns that API-key authentication does not protect every endpoint; production deployments need network controls and hardening.

## Enterprise selection questions

- Which APIs are required
- Are chat templates and tool-call formats compatible
- Are all exposed endpoints protected
- Are TLS, reverse proxy, rate limits and network isolation configured?
