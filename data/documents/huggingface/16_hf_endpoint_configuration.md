# Hugging Face Inference Endpoint configuration

- **Resource ID:** 16
- **Publisher:** Hugging Face Documentation
- **Official source:** https://huggingface.co/docs/inference-endpoints/guides/configuration
- **Purpose:** Compare hardware, authentication, PrivateLink, autoscaling and scale-to-zero.

## Reading notes

The configuration guide covers cloud provider, accelerator, region, instance memory/pricing, endpoint authentication and replica scaling. Map real traffic patterns to replica settings and weigh scale-to-zero savings against cold starts.

## Enterprise selection questions

- Should access be Private, Authenticated or Public
- Is AWS PrivateLink needed
- Are cold starts acceptable
- Do min/max replicas cover peak traffic?
