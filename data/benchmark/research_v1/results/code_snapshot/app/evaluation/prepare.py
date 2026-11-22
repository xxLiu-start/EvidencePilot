"""Freeze a reproducible corpus and agent-reviewed seed judgments for 20 queries."""

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.retrieval.ingestion import chunk_document


# Questions were reviewed against the local reading notes, not live web pages.
SEED_QUERIES = [
    ("01_qwen35_model_card.md", "Qwen3.5-35B-A3B 的多模态能力、激活参数和长上下文架构是什么？"),
    ("02_model_studio_model_catalog.md", "如何在阿里云 Model Studio 目录按模态、区域和能力筛选模型？"),
    ("03_qwen_openai_compatible_chat_api.md", "How can a Qwen application reuse OpenAI Chat Completions and handle multimodal inputs?"),
    ("04_model_studio_dedicated_deployment.md", "Model Studio 的按 token 计费与专属算力部署如何比较？"),
    ("05_deepseek_models_pricing.md", "How should DeepSeek API costs account for input, output and cache-hit token pricing?"),
    ("06_deepseek_thinking_mode.md", "DeepSeek thinking mode 的推理强度和多轮工具调用上下文需要注意什么？"),
    ("07_deepseek_tool_calls.md", "How should DeepSeek tool calls handle strict JSON Schema, retries and authorization?"),
    ("08_claude_models_overview.md", "Which Claude overview helps compare context windows, output limits and speed?"),
    ("09_claude_pricing.md", "Claude prompt caching 的缓存写入、缓存命中和稳定前缀如何影响费用？"),
    ("10_claude_api_data_retention.md", "What are the eligibility boundaries and feature exceptions of Claude API Zero Data Retention?"),
    ("11_vllm_openai_compatible_server.md", "vLLM OpenAI-compatible server 的 API key 是否保护所有端点，还需要什么网络加固？"),
    ("12_vllm_quantization.md", "How do vLLM FP8, INT4, AWQ and GPTQ choices depend on GPU compatibility and quality?"),
    ("13_vllm_parallelism_scaling.md", "vLLM 的 tensor parallelism 和 pipeline parallelism 如何结合 KV cache 与 GPU 互联规划？"),
    ("14_vllm_production_metrics.md", "Which vLLM Prometheus metrics support monitoring TTFT, TPOT, queue depth and KV cache?"),
    ("15_hf_create_endpoint.md", "创建 Hugging Face Inference Endpoint 时如何选择模型、云区域、实例和安全等级？"),
    ("16_hf_endpoint_configuration.md", "How do Hugging Face endpoint replicas, scale-to-zero and PrivateLink affect deployment?"),
    ("17_hf_security_enterprise.md", "Hugging Face Hub 的访问令牌、MFA、提交签名与模型文件扫描如何控制供应链风险？"),
    ("18_nist_ai_rmf_genai_profile.md", "What is the role of NIST AI 600-1 in generative AI risk management and lifecycle evaluation?"),
    ("19_iso_iec_42001.md", "ISO/IEC 42001 对 AI 管理体系、责任角色、供应商和生命周期控制提出什么要求？"),
    ("20_owasp_top10_llm_apps.md", "Which OWASP LLM application risks involve prompt injection, output validation and excessive agency?"),
]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def prepare_dataset(documents: Path, output: Path):
    paths = sorted(documents.rglob("*.md"))
    if not paths:
        raise ValueError("No Markdown corpus files found")
    chunks, sources = [], []
    for path in paths:
        source = path.relative_to(documents).as_posix()
        digest = sha256(path)
        document_id = str(uuid5(NAMESPACE_URL, "evidencepilot-document:" + source + ":" + digest))
        for chunk in chunk_document(str(path), document_id=document_id, source=source, version=digest[:12]):
            item = asdict(chunk)
            item["id"] = str(uuid5(NAMESPACE_URL, f"{document_id}:{chunk.chunk_index}:{chunk.text}"))
            chunks.append(item)
        sources.append({"source": source, "sha256": digest})
    queries, annotations, qrels = [], [], {}
    for i, (filename, query) in enumerate(SEED_QUERIES, 1):
        query_id = f"q{i:02d}"
        target = [c for c in chunks if Path(c["source"]).name == filename
                  and not (c["section"] or "").endswith("Enterprise selection questions")]
        if not target:
            raise ValueError(f"No judgment target for {query_id}: {filename}")
        grades = {c["id"]: 2 if (c["section"] or "").endswith("Reading notes") else 1 for c in target}
        queries.append({"id": query_id, "query": query})
        qrels[query_id] = grades
        annotations.append({
            "query_id": query_id, "query": query, "annotation_status": "agent_reviewed_pending_human",
            "rationale": "Direct reading notes answer the topic (grade 2); title/purpose identify the relevant reference (grade 1).",
            "relevant_chunks": [{"chunk_id": c["id"], "grade": grades[c["id"]],
                                 "source": c["source"], "section": c["section"], "text": c["text"]} for c in target],
        })
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "chunks.jsonl", chunks)
    write_jsonl(output / "queries.jsonl", queries)
    write_jsonl(output / "annotations.jsonl", annotations)
    (output / "qrels.json").write_text(json.dumps(qrels, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "dataset": "evidencepilot-day3-seed-v1", "annotation_status": "agent_reviewed_pending_human",
        "human_reviewed": False, "query_count": len(queries), "chunk_count": len(chunks),
        "chunk_size": 500, "chunk_overlap": 80, "tokenizer": "cl100k_base", "sources": sources,
        "limitations": ["Small local reading-note corpus, not full official documentation.",
                        "Seed judgments are agent-reviewed; human review is pending.",
                        "Cross-source relevance judgments may be incomplete; metrics are a smoke benchmark, not proof of general superiority."],
        "files": {name: sha256(output / name) for name in ["chunks.jsonl", "queries.jsonl", "qrels.json", "annotations.jsonl"]},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, default=Path("data/documents"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/day3"))
    args = parser.parse_args()
    result = prepare_dataset(args.documents, args.output)
    print(json.dumps({key: result[key] for key in ["dataset", "query_count", "chunk_count", "annotation_status"]}))


if __name__ == "__main__":
    main()
