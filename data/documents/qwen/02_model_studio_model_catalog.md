# Alibaba Model Studio model catalog

- **Resource ID:** 02
- **Publisher:** Alibaba Cloud Model Studio
- **Official source:** https://help.aliyun.com/zh/model-studio/list-models
- **Purpose:** Discover available models, capabilities, context lengths, prices, providers and deployment modes.

## Reading notes

The catalog API supports filtering by provider, modality, capability, feature, context length, region and deployment mode. It returns model IDs, descriptions, capabilities and pricing fields. Treat the catalog and prices as dynamic; re-check before procurement.

## Enterprise selection questions

- Is the model available in the required region
- Does it support tool calling and structured output
- What are context, price and rate limits
- Is it first-party or a third-party routed model?
