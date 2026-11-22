# Enterprise LLM Selection Checklist

## A. Business requirements
- [ ] Define tasks: support, knowledge Q&A, coding, document processing, agents, vision, etc.
- [ ] Prepare a representative, de-identified evaluation set.
- [ ] Define quality metrics: accuracy, factuality, task success, refusal quality and tool-call success.
- [ ] Define service metrics: P50/P95/P99 latency, throughput, concurrency and availability.
- [ ] Estimate the cost of wrong answers, wrong tool calls and human rework.

## B. Model capability
- [ ] Chinese/English and domain terminology.
- [ ] Reasoning, coding, long documents and multi-turn context.
- [ ] Vision/audio/video requirements.
- [ ] Function calling, tool use and structured output.
- [ ] Context window and maximum output.
- [ ] Refusal, safety and adversarial robustness.
- [ ] Results on the organization's own evaluation set.

## C. Deployment and infrastructure
- [ ] API hosting, dedicated deployment or fully private hosting.
- [ ] Region, data residency and network access.
- [ ] GPU type, VRAM, interconnect and compute budget.
- [ ] vLLM/SGLang/Transformers compatibility.
- [ ] Quantization quality regression.
- [ ] Parallelism, scaling, rollout and rollback.

## D. Total cost of ownership
- [ ] Input/output tokens and cache-hit assumptions.
- [ ] Peak/off-peak prices, rate limits and minimum spend.
- [ ] GPU, storage, network and operations costs.
- [ ] Cost per successful task, not just price per million tokens.
- [ ] Retries, agent turns and tool costs.
- [ ] Budget limits and abnormal-usage alerts.

## E. Security, privacy and compliance
- [ ] Whether data is used for model training and opt-out options.
- [ ] Retention, deletion and ZDR scope.
- [ ] DPA, confidentiality terms, audit rights and subprocessors.
- [ ] Authentication, RBAC, MFA, key rotation and audit logs.
- [ ] Private networking, TLS and data residency.
- [ ] Prompt injection, sensitive-data disclosure and tool privilege controls.
- [ ] Model license and supply-chain review.

## F. Vendor and operations
- [ ] SLA, support response, status and incident notification.
- [ ] Model upgrades, deprecation and compatibility policy.
- [ ] Price-change notices and contract protection.
- [ ] Vendor lock-in and migration strategy.
- [ ] Monitoring for quality, latency, tokens, cost and errors.
- [ ] Regular re-evaluation and model-change approval.

## Suggested scoring weights (adjust to the use case)
| Dimension | Weight |
|---|---:|
| Business task quality | 30% |
| Security, privacy and compliance | 20% |
| Deployment feasibility and reliability | 15% |
| Total cost of ownership | 15% |
| Latency, throughput and scalability | 10% |
| Vendor support and portability | 10% |

Do not select a model only by public benchmarks or parameter count. Compare at least 2-3 candidates on the same evaluation set, prompt, decoding configuration and load conditions.
