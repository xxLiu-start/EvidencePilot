# vLLM parallelism and scaling

- **Resource ID:** 13
- **Publisher:** vLLM Documentation
- **Official source:** https://docs.vllm.ai/en/stable/serving/parallelism_scaling/
- **Purpose:** Plan single-node multi-GPU and distributed inference.

## Reading notes

The guide covers tensor parallelism, pipeline parallelism and distributed execution. The right strategy depends on model architecture, GPU memory, interconnect and topology. Include KV cache, context length and concurrency in capacity planning.

## Enterprise selection questions

- What are total weight and KV-cache memory needs
- Is NVLink available
- Is single-node or multi-node serving appropriate
- What do P95/P99 latency and throughput targets imply?
