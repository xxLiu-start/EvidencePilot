# EvidencePilot business retrieval evaluation data

This directory intentionally contains no customer documents, queries, or labels.
Keep private or tenant-confidential evaluation data in an access-controlled
local workspace or encrypted CI artifact; do not commit it to this repository.

Prepare three UTF-8 JSONL files using a de-identified, held-out sample:

- queries.jsonl: {"id":"q001","text":"..."}
- corpus.jsonl: {"id":"doc001","text":"..."}
- qrels.jsonl: {"query_id":"q001","doc_id":"doc001","relevance":2}

Use relevance grades 0 (not relevant), 1 (partially relevant), and 2 (highly relevant). Include judged non-relevant candidate documents as grade 0. Ensure every qrels query/document ID exists in the corresponding file. Query IDs and document IDs must be unique. Have a domain reviewer label relevance independently of the model outputs.

Recommended initial sample: at least 100 held-out queries and 1,000 representative chunks, stratified by document type, language, tenant/project and query difficulty. Do not mix train/dev/test labels. Keep all chunks for a source document within one split to prevent leakage. Never use data from one tenant to evaluate retrieval for another tenant.

Run from the repository root after provisioning the official, integrity-verified local BGE-M3 and reranker bundles and setting the production model environment:

    python -m scripts.retrieval_quality_benchmark \
      --queries evaluation/bge_m3/business/queries.jsonl \
      --corpus evaluation/bge_m3/business/corpus.jsonl \
      --qrels evaluation/bge_m3/business/qrels.jsonl \
      --candidate-k 50 --top-k 10 \
      --output reports/bge_m3/business-retrieval-metrics.json

The report compares BM25, BGE-M3 Dense, Dense+BM25 RRF and Hybrid+Reranker using Recall@10, MRR@10 and NDCG@10. It is an offline evaluation and does not modify the production index or original benchmark evaluator.

For release evidence, also record the data extraction timestamp, anonymization procedure, dataset checksum, model revisions and manifest digests, code commit, package versions, hardware, per-query latency, and bootstrap 95% confidence intervals. The initial benchmark script reports aggregate ranking metrics only; add a separate latency/CI report before using this evaluation as a production SLO sign-off.
