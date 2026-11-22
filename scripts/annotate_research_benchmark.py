"""Initial independently authored rubrics; MUST NOT claim human-approved truth."""
from collections import Counter
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.search.cache import digest, write_json
from app.evaluation.research_dataset import make_chunks

DEST = ROOT / "data/benchmark/research_v1"


def build(pages):
    sources = {page["source_id"]: page for page in pages}
    def ref(source, anchor, length=240, relation="supports"):
        body = sources[source]["body"]
        start = body.index(anchor)
        return {"source_id": source, "source_url": sources[source]["url"], "supporting_text": body[start:start+length],
                "quote_start": start, "quote_end": min(start+length, len(body)), "relation": relation}
    def claim(text, patterns, refs, *, status="SUPPORTED", groups=None):
        return {"claim": text, "expected_status": status, "answer_patterns": patterns,
                "reference_evidence": refs, "required_source_groups": groups or [[r["source_id"]] for r in refs]}
    bgedim = ref("bge", "| [BAAI/bge-m3](")
    bgelen = ref("bge", "long documents of up to 8192 tokens")
    hnsw = ref("pgvector", "Also, an index can be created without any data")
    ivf = ref("pgvector", "An IVFFlat index divides vectors")
    after = ref("fastapi", "You can define background tasks")
    multi = ref("bge", "It can support more than 100 working languages")
    rermulti = ref("reranker", "[BAAI/bge-reranker-v2-m3](")
    score = ref("reranker", "directly output similarity instead of embedding")
    pipe = ref("bge", "We recommend to use the following pipeline")
    vector = ref("pgvector", "Create a vector column with 3 dimensions")
    cosine = ref("pgvector", "Also supports inner product")
    short = ref("langgraph", "Checkpointers persist")
    long = ref("langgraph", "Stores persist")
    ram = ref("langgraph", "MemorySaver and InMemorySaver")
    celery = ref("fastapi", "you might benefit from using other bigger tools like Celery")
    fsync = ref("pgwal", "If this parameter is on, the PostgreSQL server")
    asyncsafe = ref("pgasync", "The risk that is taken by using asynchronous commit")
    asyncrisk = ref("pgasync", "Asynchronous commit provides behavior different")
    q7 = ref("qwen7", "Context Length: Full 131,072 tokens")
    q1 = ref("qwen1", "Context Length: Full 32,768 tokens")
    optional = ref("pydantic", "A field annotated as `typing.Optional[T]`")
    default = ref("pydantic", "Any default value if provided makes a field not required")
    old = ref("oldqwen", "support up to 128K tokens", relation="outdated")
    oldvec = ref("oldvector", "# pgvector", relation="outdated")
    tasks = []
    def add(category, query, claims, allowed=None):
        for number, item in enumerate(claims, 1):
            item["id"] = f"C{number}"
        statuses = {item["expected_status"] for item in claims}
        tasks.append({"task_id": f"research_{len(tasks)+1:03d}", "category": category, "query": query,
                      "required_claims": claims, "reference_evidence": [r for c in claims for r in c["reference_evidence"]],
                      "expected_status": "answerable" if statuses == {"SUPPORTED"} else "outdated" if statuses == {"OUTDATED"} else "unanswerable",
                      "allowed_source_ids": allowed or list(sources),
                      "evaluation_rubric": {"requires_official_source": True, "must_cite_each_claim": True,
                          "judge": "deterministic_status_patterns_and_exact_quote_qrels_v1", "human_reviewed": False}})
    add("single_source", "BAAI/bge-m3 的 Dense 向量维度是多少？", [claim("BGE-M3 Dense 向量维度", [r"1024"], [bgedim])])
    add("single_source", "BAAI/bge-m3 模型卡给出的最大输入长度是多少 tokens？", [claim("BGE-M3 最大输入长度", [r"8192|8,192"], [bgelen])])
    add("single_source", "pgvector v0.8.0 的 HNSW 是否必须先有训练数据才能建索引？", [claim("HNSW 不需要训练数据即可创建", [r"不需要|无需|without|no training"], [hnsw])])
    add("single_source", "FastAPI BackgroundTasks 在响应返回前还是返回后运行？", [claim("后台任务在响应返回后执行", [r"后|after"], [after])])
    add("multi_source", "依据两个官方模型卡，核对 BGE-M3 和 bge-reranker-v2-m3 是否都支持多语言。", [claim("BGE-M3 支持多语言", [r"多语|multilingual|100"], [multi]), claim("bge-reranker-v2-m3 支持多语言", [r"多语|multilingual"], [rermulti])])
    add("multi_source", "用 pgvector 存储 BGE-M3 Dense 向量应声明多少维？再核对 pgvector 是否支持余弦距离。", [claim("BGE-M3 应存为 1024 维", [r"1024"], [bgedim, vector], groups=[["bge"], ["pgvector"]]), claim("pgvector 支持余弦距离", [r"余弦|cosine|<=>"], [cosine])])
    add("multi_source", "比较 FastAPI BackgroundTasks 与 LangGraph 的内存检查点：是否适合持久恢复，重启后内存检查点是否保留？", [claim("BackgroundTasks 同进程适用，小任务可用，复杂任务可选 Celery", [r"Celery|同.*进程|same process"], [celery]), claim("MemorySaver/InMemorySaver 重启会丢失", [r"丢失|不.*保留|lost|RAM|内存"], [ram])])
    add("multi_source", "BGE-M3 建议的 RAG 检索流水线是什么？bge-reranker 输出向量还是相关性分数？", [claim("建议 hybrid retrieval 加 reranking", [r"hybrid|混合", r"rerank|重排|重排序"], [pipe]), claim("reranker 输出相关性分数而非 embedding", [r"分数|score|similarity"], [score])])
    add("multi_hop", "先查 BGE-M3 Dense 维度，再推导 pgvector 建表列类型并给出余弦距离运算符。", [claim("维度对应 vector(1024)", [r"vector\s*\(\s*1024\s*\)"], [bgedim, vector], groups=[["bge"], ["pgvector"]]), claim("余弦距离使用 <=>", [r"<=>"], [cosine])])
    add("multi_hop", "研究服务需响应后执行，并在进程重启后保留任务状态：FastAPI 怎么安排工作，LangGraph 的内存 Checkpointer 是否足够？", [claim("BackgroundTasks 响应后执行", [r"后|after"], [after]), claim("内存 Checkpointer 不够，需要持久化 Checkpointer", [r"PostgresSaver|SqliteSaver|持久|persistent"], [ram])])
    add("multi_hop", "为中文英文混合资料设计检索：BGE-M3 是否支持这类语言输入，召回后 bge-reranker 如何计算候选相关性？", [claim("BGE-M3 多语言支持", [r"多语|multilingual|100"], [multi]), claim("reranker 对 query 与 passage 输入输出分数", [r"query|查询", r"passage|文本|文档|候选", r"分数|score|相关"], [score])])
    add("multi_hop", "已提交任务状态需要崩溃安全：PostgreSQL 异步提交与 fsync=off 的风险相同吗，fsync 开启如何保护落盘？", [claim("异步提交可能丢近期事务但不会因此损坏数据库", [r"丢失|loss", r"不.*损坏|not.*corruption|一致"], [asyncsafe]), claim("fsync off 可能损坏，开启强制物理写盘", [r"fsync", r"损坏|corrupt|落盘|disk"], [asyncrisk, fsync], groups=[["pgasync"], ["pgwal"]])])
    add("source_conflict", "两条官方资料分别写 131072 和 32768：核对 Qwen2.5-7B-Instruct 与 Qwen2.5-1.5B-Instruct 的完整上下文长度，解释是否同型号冲突。", [claim("7B 完整上下文 131072", [r"131.?072|128\s*[kK]"], [q7]), claim("1.5B 完整上下文 32768，型号不同不构成同型号矛盾", [r"32.?768|32\s*[kK]", r"不同|不.*冲突|different"], [q1, q7], groups=[["qwen1"], ["qwen7"]])])
    add("source_conflict", "同一份 pgvector 文档同时说不需训练和要训练，这矛盾吗？区分 HNSW 与 IVFFlat。", [claim("HNSW 无训练步骤", [r"无需|不需要|without|no training"], [hnsw]), claim("IVFFlat 需要训练，属于不同索引类型", [r"训练|training", r"不同|different|IVFFlat"], [ivf])])
    add("source_conflict", "有人说 Optional[str] 可省略，也有人说必须提供。依据 Pydantic v2 迁移文档解释版本变化及添加 = None 后的行为。", [claim("V2 Optional 无默认值仍必填，可取 None，与 V1 不同", [r"必填|required|必须", r"V1|版本|变化|breaking"], [optional]), claim("显式默认 None 后可省略", [r"None", r"省略|不.*必填|not required|可选|默认"], [default, optional], groups=[["pydantic"]])])
    add("outdated", "仅依据所给 2024 年 Qwen2.5 发布公告，确认 2026 年 Qwen3.8 当前完整上下文长度。若依据过期应标 OUTDATED。", [claim("旧 Qwen2.5 公告不能验证当前 Qwen3.8 上下文", [], [old], status="OUTDATED")], ["oldqwen"])
    add("outdated", "仅依据 pgvector v0.7.0 历史 README，确认 v0.8.0 iterative_scan 配置行为。旧版本不足请标 OUTDATED。", [claim("0.7.0 快照不能确认 0.8.0 iterative_scan", [], [oldvec], status="OUTDATED")], ["oldvector"])
    add("outdated", "仅依据 2024 年 Qwen2.5 发布公告，确认 2026 年当前 Qwen3.8 企业部署许可条款。请检查日期和型号，依据过期标 OUTDATED。", [claim("旧版本公告不足以确认 Qwen3.8 当前许可", [], [old], status="OUTDATED")], ["oldqwen"])
    add("unanswerable", "官方技术文档快照中，EvidencePilot 私有客户 Acme 的生产数据库密码是什么？没有直接证据必须弃答。", [claim("没有 Acme 私有数据库密码证据", [], [], status="INSUFFICIENT")])
    add("unanswerable", "仅凭这些公开技术文档，给出 EvidencePilot 未发布的内部生产压测 QPS 精确值；无直接证据必须弃答。", [claim("没有未公开生产 QPS 数值证据", [], [], status="INSUFFICIENT")])
    # Separate neutral research requirements from hidden reference assertions.
    # The evaluated model must never receive answer-bearing label text.
    neutral = [
        ["BGE-M3 Dense 向量维度是多少？"], ["BGE-M3 最大输入长度是多少？"],
        ["HNSW 建索引是否需要训练数据？"], ["BackgroundTasks 在响应前还是响应后执行？"],
        ["BGE-M3 是否支持多语言？", "bge-reranker-v2-m3 是否支持多语言？"],
        ["BGE-M3 在 pgvector 中应使用多少维？", "pgvector 是否支持余弦距离？"],
        ["FastAPI 后台任务适用范围及复杂任务方案是什么？", "LangGraph 内存 Checkpointer 重启后状态如何？"],
        ["BGE-M3 推荐什么检索流水线？", "reranker 的输出形式是什么？"],
        ["BGE-M3 维度对应的 pgvector 列类型是什么？", "余弦距离运算符是什么？"],
        ["FastAPI 如何安排响应后的工作？", "如何让 LangGraph 状态在重启后保留？"],
        ["BGE-M3 能否处理中文英文资料？", "reranker 如何使用查询与候选文本计算相关性？"],
        ["异步提交在崩溃后有什么风险？", "fsync 开关如何影响崩溃安全和物理落盘？"],
        ["Qwen2.5-7B-Instruct 完整上下文是多少？", "Qwen2.5-1.5B-Instruct 上下文是多少，与 7B 是否为同型号冲突？"],
        ["HNSW 是否有训练步骤？", "IVFFlat 的训练要求与 HNSW 是否矛盾？"],
        ["Pydantic V2 Optional 无默认值的必填行为与 V1 有何区别？", "添加显式默认 None 后能否省略字段？"],
        ["给定旧公告能否验证当前 Qwen3.8 上下文？"], ["给定旧版本能否确认目标版本 iterative_scan 行为？"],
        ["给定旧公告能否确认当前 Qwen3.8 部署许可？"], ["公开快照能否给出该私有密码？"], ["公开文档能否确定未发布的生产 QPS？"],
    ]
    for task, requirements in zip(tasks, neutral, strict=True):
        for item, requirement in zip(task["required_claims"], requirements, strict=True):
            item["reference_statement"], item["claim"] = item["claim"], requirement
    tasks[12]["required_claims"][0]["reference_evidence"].append(ref("qwen7", "**Long-context Support** up to 128K"))
    tasks[12]["required_claims"][1]["reference_evidence"].extend([
        ref("qwen7", "**This repo contains the instruction-tuned 7B"),
        ref("qwen1", "**This repo contains the instruction-tuned 1.5B")])
    for task in tasks:
        task["reference_evidence"] = [ref for item in task["required_claims"] for ref in item["reference_evidence"]]
    return tasks


def main():
    if (DEST/"results/runs.jsonl").exists():
        raise SystemExit("A scored dataset is immutable; create a new dataset version for label changes.")
    page_file = DEST / "sources.json"
    sources = json.loads(page_file.read_text())["sources"]
    # Record the actual canonical page reached by the documented redirect.
    for page in sources:
        if page["source_id"] == "langgraph":
            page["url"], page["title"] = page["fetched_url"], "LangGraph persistence"
        if page["source_id"] == "oldvector":
            page["published_at"] = "2024-04-29T16:26:06+00:00"
            page["published_at_provenance"] = "Official GitHub git commit timestamp for tag v0.7.0"
            page["scope"] = {"model_name": "pgvector", "model_version": "0.7.0", "deployment_mode": "extension"}
        if page["source_id"] == "oldqwen":
            page["published_at"] = "2024-09-19T00:00:04+08:00"
            page["published_at_provenance"] = "Official webpage article:published_time and JSON-LD datePublished"
    write_json(page_file, {"sources": sources})
    tasks = build(sources)
    assert Counter(task["category"] for task in tasks) == {"single_source": 4, "multi_source": 4,
        "multi_hop": 4, "source_conflict": 3, "outdated": 3, "unanswerable": 2}
    write_json(DEST / "tasks.json", {"tasks": tasks})
    write_json(DEST / "chunks.json", {"chunks": make_chunks(sources)})
    write_json(DEST / "manifest.json", {"dataset_version": "research-v1", "task_count": len(tasks),
        "categories": dict(Counter(t["category"] for t in tasks)), "annotation_status": "agent_reviewed_pending_human",
        "human_reviewed": False, "annotation_author": "independent source/rule review, not the evaluated model",
        "created_at": datetime.now(timezone.utc).isoformat(), "evaluation_as_of": "2026-11-20T08:00:00+00:00",
        "files": {name: hashlib.sha256((DEST/name).read_bytes()).hexdigest() for name in ("sources.json", "tasks.json", "chunks.json")},
        "limitations": ["Initial labels require human confirmation before a formal superiority claim.",
            "Small engineering-document task suite; no external validity claim.",
            "Frozen-corpus web search measures cached-document research, not live search-engine quality.",
            "Rule-based accuracy checks may miss semantic errors; human review remains necessary."]})
    print("Sealed 20 tasks; human_reviewed=false", flush=True)


if __name__ == "__main__":
    main()
