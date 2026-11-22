# vLLM quantization and hardware compatibility

- **Resource ID:** 12
- **Publisher:** vLLM Documentation
- **Official source:** https://docs.vllm.ai/en/stable/features/quantization/
- **Purpose:** Assess FP8, INT8, INT4, AWQ and GPTQ trade-offs across hardware.

## Reading notes

The guide lists supported quantization formats and hardware compatibility. Quantization can reduce memory requirements but may affect quality and throughput. Benchmark the target model, GPU, precision and concurrency together.

## Enterprise selection questions

- Does the GPU architecture support the selected kernel
- Is quality loss acceptable
- Does the quantized model meet memory and KV-cache targets
- Is the checkpoint from a trusted source?
