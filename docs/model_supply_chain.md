# Production model supply chain

Production embedding and reranking require an administrator-provisioned absolute local model directory, a 40-character upstream commit revision, local-files-only mode, and an independently trusted SHA-256 of manifest.json. The manifest maps every relative file path to its SHA-256 and includes the revision. Mount the bundle read-only, disable network egress for model inference, and keep manifest digests in trusted deployment configuration. Never generate trust anchors from an unreviewed download at startup.

Only JSON, safetensors, tokenizer model and text files are permitted. Pickle-based weights, Python code, symbolic links and unlisted files are rejected. Some BGE-M3 releases need auxiliary .pt weights; do not silently allow those files. A compatible reviewed safetensors-only bundle or an isolated replacement dense encoder must be validated before release. This is an integrity check, not a sandbox or proof of model provenance.

Set EMBEDDING_MODEL, EMBEDDING_REVISION, EMBEDDING_LOCAL_FILES_ONLY, EMBEDDING_MANIFEST_SHA256 and corresponding RERANKER variables. The model loaders verify the bundle before construction. The development loader remains unchanged.

Run actual inference with the configured read-only bundles and DATABASE_URL=sqlite:// ENVIRONMENT=production HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m scripts.model_acceptance. This validates vector dimensions, normalization, finite output and basic semantic ranking; it is not a full retrieval quality benchmark. Record the exact commit, dependency versions, artifact hashes and output. No production release until the real smoke test and retrieval regression benchmark pass.

Model runtime isolation, container scans and independent security sign-off remain separate release gates.
