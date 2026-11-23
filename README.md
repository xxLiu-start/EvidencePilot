# EvidencePilot

EvidencePilot 是一个以 Claim 和可追溯 Evidence 为中心的研究 Agent。
它将问题拆解为 Claims，依据来源、覆盖率、冲突、时效和剩余预算选择 Web Search 或 RAG，
对原文证据显式验证；无法验证的部分保留缺口并弃答。当前为本机开发 MVP。

Streamlit 提供研究工作区、文档上传、Agent Trace、Evidence Explorer、研究报告和评测看板。
本地启动细节见 [start.md](start.md)，本页末尾给出部署、演示和最终验收说明。

## 系统架构与技术栈

```mermaid
flowchart LR
    User[研究者] --> UI[Streamlit 六页面]
    UI -->|HTTP API / 轮询| API[FastAPI]
    API --> Graph[LangGraph Agent]
    Graph --> Planner[Claim Planner]
    Planner --> Router[LLM 提议 + 预算规则]
    Router --> Tools[Web Search / RAG / Open / Verify]
    Tools --> Evidence[原文校验 / 来源 / 时效 / 冲突]
    Evidence --> Coverage[Coverage 更新]
    Coverage -->|需要更多证据| Router
    Coverage -->|充分或预算耗尽| Report[报告 / 弃答]
    Report --> API
    Tools --> Tavily[Tavily + 网页正文缓存]
    Tools --> Retrieval[BGE-M3 / BM25 / RRF / Reranker]
    API --> PG[(PostgreSQL + pgvector)]
    Graph --> PG
    Retrieval --> PG
    Graph --> LLM[统一推理客户端]
    LLM -->|local| Local[独立 Qwen 服务]
    LLM -->|remote| Remote[HTTPS Chat Completions]
```

同一 API 进程只选择一个推理分支；故障不会转发到另一分支。
Streamlit 不读取 PostgreSQL、模型 Key 或 Tavily Key，全部业务数据来自 FastAPI。

| 层次 | 技术 |
| --- | --- |
| 界面与服务 | Streamlit、FastAPI、Pydantic、httpx |
| Agent 与推理 | LangGraph、OpenAI-compatible Chat Completions；本轮主模型 Qwen3.8-27B |
| 检索 | BGE-M3 1024 维、PostgreSQL/pgvector、rank-bm25/jieba、RRF、BGE Reranker |
| 网页与文档 | Tavily、trafilatura、pypdf、python-docx、tiktoken |
| 持久化与部署 | SQLAlchemy、PostgreSQL 16、Docker Compose、Conda Python 3.12 |
| 验证 | pytest、Streamlit AppTest、真实 Chromium/Playwright |

## 数据库 Schema

完整字段、约束和外键见 [app/db/models.py](app/db/models.py)，启动迁移见 [app/db/schema.py](app/db/schema.py)。

| 表 | 保存内容 |
| --- | --- |
| research_tasks / agent_runs | 任务、预算、当前状态、租约、恢复游标 |
| documents / document_chunks | 文档元数据、Chunk 正文、page/section/source/version、vector(1024) |
| claims / evidences | Claim、逐字证据片段、来源 URL/文档、Hash、位置与范围 |
| web_sources / web_contents / web_searches | 规范化 URL、正文去重缓存、搜索 Query 与耗时 |
| agent_steps / agent_policy_decisions / agent_reflections | Action–Observation、策略检查、失败反思和改写 |
| evidence_assessments | 按任务、Claim 和证据指纹保存的验证结果 |
| state_snapshots / tool_execution_receipts | 每步快照及已提交工具回执 |
| task_feedback / evaluation_results | 用户反馈与冻结评测结果 |

```mermaid
erDiagram
    RESEARCH_TASKS ||--o| AGENT_RUNS : state
    RESEARCH_TASKS ||--o{ AGENT_STEPS : executes
    RESEARCH_TASKS ||--o{ STATE_SNAPSHOTS : checkpoints
    RESEARCH_TASKS ||--o{ EVIDENCE_ASSESSMENTS : verifies
    AGENT_STEPS ||--o| TOOL_EXECUTION_RECEIPTS : committed_result
    DOCUMENTS ||--o{ DOCUMENT_CHUNKS : contains
    DOCUMENT_CHUNKS ||--o{ EVIDENCES : quoted_by
    CLAIMS ||--o{ EVIDENCES : concerns
    WEB_CONTENTS ||--o{ WEB_SOURCES : cached_body
    WEB_CONTENTS ||--o{ EVIDENCES : original_body
```

Claim/Evidence 与任务的归属还通过 `agent_runs.state` 中的 ID 与完整 Claim 文本校验，
不能仅按不同任务中可能重名的 `C1` 查询。Evidence Explorer 使用任务范围接口。

## 文档分块与元数据

支持 PDF、DOCX、Markdown 和 UTF-8 TXT。默认使用 `cl100k_base` tokenizer，
每块最多 **500 tokens**，同一页、同一章节的相邻块重叠约 **80 tokens**。
优先按标题划分章节，再选择段落边界；超长段落按 token 切分，并保持中文、emoji 的完整字符。
章节或页面边界处不添加重叠，因此短章节和短页可以产生较小的块。

每个块保存到 `document_chunks` 表，包含：

| 字段 | 含义 |
| --- | --- |
| `document_id` | 所属文档的 UUID |
| `page` | PDF 的真实页码，从 1 开始；DOCX、MD、TXT 为 `null` |
| `section` | 标题路径，例如 `Guide > Setup`；无标题时为 `null` |
| `source` | 调用方指定的来源，上传时默认使用原文件名 |
| `version` | 调用方指定的版本，默认 `1` |
| `chunk_index` | 文档内的块序号，从 0 开始 |
| `text` / `token_count` | 块正文及其实际 token 数 |

Markdown 识别 `#` 标题、下划线标题和标题层级，代码围栏内的 `#` 不作为标题。
DOCX 识别 Word 的 `Heading 1`–`Heading 9` 样式。
PDF/TXT 对独立的数字编号标题和“第一章 …”等标题作启发式识别；PDF 跨页继承章节。
扫描版 PDF 暂不执行 OCR，无可提取文字时返回 `422`。

### 上传并保存分块

更新依赖并重启 API 后，启动流程会创建缺失的 `documents`、`document_chunks` 表。
安装依赖及首次准备 tokenizer：

```bash
python -m pip install -r requirements.txt
python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
```

首次准备 tokenizer 需要下载公开词表，缓存后可离线分块。Docker 构建时已预缓存词表。

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/documents \
  -F 'file=@/path/to/guide.md' \
  -F 'source=https://example.com/guide' \
  -F 'version=v2' \
  -F 'chunk_size=500' \
  -F 'chunk_overlap=80'
```

`POST /api/v1/documents` 成功返回 `201`，响应包含文档 `id` 和 `chunk_count`。
只有 `file` 必填，其余表单字段可省略。要求 `0 <= chunk_overlap < chunk_size`。
每次上传生成新的文档 ID；文档与所有分块在同一个事务中保存。上传的原始文件仅在解析期间临时保存。
上传现在会先生成 BGE-M3 dense 向量，再将正文、元数据和向量一并入库；模型不可用时返回 `503`，不会保存半成品文档。

复制返回的 `id` 后读取有序分块：

```bash
curl --noproxy 127.0.0.1 -fsS \
  http://127.0.0.1:8001/api/v1/documents/<document_id>/chunks
```

### 在 Python 中使用

```python
from dataclasses import asdict
from app.retrieval.ingestion import chunk_document

chunks = chunk_document(
    "/path/to/guide.md",
    document_id="my-document",
    source="https://example.com/guide",
    version="v2",
)
print(asdict(chunks[0]))
```

`chunk_document` 仅解析和分块。需要保存到数据库时，使用
`app.db.repository.ingest_document(db, path, source=..., version=...)`，由它生成文档 UUID。
原有的 `parse_document(path) -> str` 接口仍可使用。

## BGE-M3 Embedding 与 pgvector 入库

使用官方 [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) 的 **1024 维 dense 向量**，
支持中英文。向量经过 L2 归一化，保存为 PostgreSQL 的 `vector(1024)`；
按 [pgvector 的方式](https://github.com/pgvector/pgvector-python#sqlalchemy) 建立余弦距离 HNSW 索引。
每个分块额外保存 `embedding_model`。分块查询返回 `embedding_model`、
`embedding_dimensions`，正文查询响应不包含完整向量数组。

模型按需加载，同一进程复用模型，按批推理。CPU/FP32 为默认配置。
`.env.example` 提供了以下可选配置，可以加入已有 `.env`：

```dotenv
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DEVICE=cpu
EMBEDDING_USE_FP16=false
EMBEDDING_BATCH_SIZE=16
EMBEDDING_MAX_LENGTH=8192
EMBEDDING_CACHE_DIR=data/cache/models
EMBEDDING_LOCAL_FILES_ONLY=false
```

使用 GPU 时可改为 `EMBEDDING_DEVICE=cuda:0` 和 `EMBEDDING_USE_FP16=true`。
`EMBEDDING_MODEL` 也可以指向本地 BGE-M3 模型目录；数据库维度固定为 1024。
首次调用会下载模型，缓存位置由 `EMBEDDING_CACHE_DIR` 指定。
只下载 PyTorch 推理所需文件，跳过 ONNX 导出。缓存完整后可设置
`EMBEDDING_LOCAL_FILES_ONLY=true`，让模型加载仅使用本地文件。
需要提前下载并验证模型时，在已配置数据库连接的项目目录运行：

```bash
python -c 'from app.retrieval.embedding import get_embedding_service; print(len(get_embedding_service().embed_query("文档检索测试")))'
```

成功应输出 `1024`。生产环境可将缓存目录挂载为持久卷，避免重建容器后重复下载权重。

### 新文档入库

现有 `POST /api/v1/documents` 上传接口自动执行解析、分块、向量化和入库。
也可以从命令行导入文件或目录：

```bash
python -m app.retrieval.index_documents /path/to/guide.pdf --version=v2
python -m app.retrieval.index_documents data/documents --recursive
```

每个文档独立提交事务。重复导入同一文件会生成新文档 ID。
命令输出 JSON，包含 `document_id`、`source`、`chunk_count`。

### 旧数据库升级与向量补算

API 启动或入库命令会幂等地启用 `vector` 扩展，创建缺失表，为已有 `document_chunks`
添加 `embedding`、`embedding_model` 列并创建 HNSW 索引。数据库连接用户需要相应 DDL 权限。
现有正文和元数据保留，旧分块的向量字段暂为 `NULL`。执行以下命令补算：

```bash
python -m app.retrieval.index_documents --backfill --batch-size=16
```

补算按批提交，失败回滚当前批次，可重复执行；已有完整向量和模型标识的分块会跳过。
补算不会自动重算已有的其他模型向量。

## Top-5 相关 Chunk 检索

`POST /api/v1/search` 默认使用同一 BGE-M3 编码查询，通过 PostgreSQL 的余弦距离检索。
默认返回最多 5 个结果，按相似度从高到低排列；向量及模型标识完整且与当前模型相同的
分块才参与检索。可检索分块不足 5 个时返回全部，空库返回空列表。

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"PostgreSQL 如何使用 pgvector 进行向量检索？","top_k":5}'
```

请求中的 `query` 必填；`top_k` 可省略，取值为 1–100。
可添加 `document_id` 将检索限制到单个文档。
响应包含 `query`、`top_k` 和 `results`。每个结果包含 Chunk 正文、原有元数据、模型标识和
`score`（余弦相似度，越大越相关），不返回完整向量数组。
查询为空或参数无效时返回 `422`，模型不可用时返回 `503`。

文档、分块和向量保存在 PostgreSQL。重启 API 后会重新连接数据库，并在第一次查询时
加载 BGE-M3，随后直接检索已入库向量。

## 当天验收

`scripts/acceptance.py` 使用真实模型、真实 PostgreSQL 和两个独立 Uvicorn 进程验证：

| 验收项 | 验证方式 |
| --- | --- |
| PDF、DOCX、Markdown 解析 | 生成三种文件，经上传接口检查正文和元数据 |
| 按设定参数切分 | 设置 160 tokens / 24 overlap，检查实际上限和相邻块重叠 |
| BGE-M3 输出 1024 维 | 使用本地真实模型，检查向量维度、有效值和归一化 |
| Chunk 和向量入库 | 从 PostgreSQL 读取并核对正文、向量和数量 |
| 返回 Top-5 相关 Chunk | 真实查询检查 5 个结果、相似度排序和相关正文 |
| 重启后仍可检索 | 停止 API 进程，启动新进程，检查原有 Chunk ID 和分数 |

先按照上文准备本地模型缓存，然后对可创建临时 schema 的 PostgreSQL 测试数据库执行：

```bash
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python scripts/acceptance.py
```

验收脚本在隔离 schema 中操作，结束时清理测试数据；生成的 JSON 报告默认位于
`data/cache/acceptance/latest.json`，可通过 `--report /path/to/report.json` 更改位置。
测试 API 使用随机本机端口，结束后自动停止，模型权重不会重复下载。

### 测试

```bash
python -m pytest -q
```

默认测试使用隔离 SQLite 和模型替身，不下载权重。真实 pgvector 测试需要单独指定测试数据库：

```bash
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python -m pytest tests/test_pgvector.py -q
```

每个 PostgreSQL 测试创建并删除自己的临时 schema，测试账号需要建 schema 和启用 `vector` 扩展的权限。

## Day 3：BM25、Hybrid、RRF 与 Cross-Encoder

检索接口支持四种 `method`，默认仍为 `dense`：

| method | 流程 | 最终 score |
| --- | --- | --- |
| `dense` | BGE-M3 → PostgreSQL 余弦距离 | 余弦相似度 |
| `bm25` | 英文规范化 / jieba 中文分词 → BM25Okapi | BM25 原始分数 |
| `hybrid` | Dense Top-10 + BM25 Top-10 → 按 Chunk ID 去重 → RRF Top-20 | RRF 分数 |
| `hybrid_rerank` | Hybrid 候选 → BGE Cross-Encoder → Top-5 | sigmoid 归一化分数 |

RRF 使用从 1 开始的排名：`sum(1 / (60 + rank))`，同一检索器的重复 ID 只计一次。
融合不比较 Dense 与 BM25 的原始分数。候选数不足时返回实际结果。
Cross-Encoder 使用官方 [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3)，
分数用于排序，不代表经过校准的正确率或概率。

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"如何评估 vLLM 的量化与硬件兼容性？","method":"hybrid_rerank","top_k":5,"candidate_k":10,"rrf_k":60}'
```

`candidate_k` 是每个召回器的候选数，默认 10，范围 1–100；`rrf_k` 默认 60，必须为正数。
响应通过 `method`、`score_type` 表示评分方式，并返回 `dense_score`、`bm25_score`、
`rrf_score`、`reranker_score`。没有参与的阶段分数为 `null`。
请求重排但模型不可用时返回 `503`，不会静默切换为其他检索方法。

### BM25 的生命周期

BM25 索引是内存对象。每个 API 进程启动时从 PostgreSQL 读取正文和元数据并重建，
不把数据库连接或 ORM 会话保存在索引中。
通过 ORM 新增、修改、删除 Chunk 时，会在同一事务内更新 `retrieval_index_revision`。
各进程在下一次 BM25 查询时检查版本并刷新，因此上传后无需重启。
事务回滚也会回滚版本变更。

如果从外部直接执行 SQL 修改 Chunk，调用以下接口递增版本并重建；其他进程会在下一次查询时刷新：

```bash
curl --noproxy 127.0.0.1 -fsS -X POST http://127.0.0.1:8001/api/v1/search/rebuild-bm25
```

### Reranker 配置

默认 CPU / FP32、batch size 4、最大长度 1024 tokens，模型延迟加载并在进程内复用。
首次运行下载到项目缓存；显存充足时可自行配置 GPU / FP16。

```dotenv
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_REVISION=main
RERANKER_DEVICE=cpu
RERANKER_USE_FP16=false
RERANKER_BATCH_SIZE=4
RERANKER_MAX_LENGTH=1024
RERANKER_CACHE_DIR=data/cache/models
RERANKER_LOCAL_FILES_ONLY=false
```

模型准备完成后，可设置 `RERANKER_LOCAL_FILES_ONLY=true` 离线运行。
`EMBEDDING_REVISION`、`RERANKER_REVISION` 可指定 Hub commit，以锁定实验模型版本。

### 可复现的四方案评测

使用 `data/documents` 中的本地阅读笔记冻结 80 个 Chunk、20 条中英文 Query。
初始标注由代理逐条审阅，已获准用于首轮实测，状态明确保存为
`agent_reviewed_pending_human`，不代表人工复核已完成。
每条标注包含相关 Chunk ID、分级相关性、原文和判定理由。

```bash
# 只有需要重新冻结语料和初始标注时才运行，已有评测集可直接复用。
python -m app.evaluation.prepare

# 先按上文准备两套模型缓存，再指定独立测试数据库。
export EVIDENCEPILOT_TEST_POSTGRES_URL='postgresql+psycopg://user:password@localhost:5432/test_db'
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 EMBEDDING_LOCAL_FILES_ONLY=true \
  RERANKER_LOCAL_FILES_ONLY=true HF_HUB_OFFLINE=1 python -m app.evaluation.benchmark

# 检查真实 API、索引更新/重建、RRF 分数、Cross-Encoder、重启及评测产物。
python scripts/acceptance_day3.py
```

评测在临时 schema 中运行并自动清理。Dense 使用精确距离和 Chunk ID 稳定排序，
Dense/BM25 各取 10 个候选，RRF 常数为 60，融合池最多 20 个。
四个方法统一输出 Top-10：Recall@5 使用所有正相关标注作为分母，
MRR 在该 Top-10 深度上计算，nDCG@10 使用分级增益 `2^grade - 1`。
表格按 Query 做宏平均。每 Query 的排名和分阶段分数都会保存。

产物位于 `data/benchmark/day3`：

| 文件 | 内容 |
| --- | --- |
| `chunks.jsonl` / `manifest.json` | 冻结正文、确定性 UUID、源文件与数据哈希、分块参数 |
| `queries.jsonl` | 20 条 Query |
| `qrels.json` / `annotations.jsonl` | 相关 Chunk ID、相关性分级、原文及初始标注状态 |
| `results/results.csv` | 四方案实测表 |
| `results/results.json` | 指标、模型 commit、配置、代码哈希、依赖版本和运行信息 |
| `results/rankings.jsonl` | 每 Query、每方法的实际排名、分数和指标 |
| `results/error_analysis.json` | 掉分 Query 的排名、原文和疑似标注缺口 |
| `acceptance.json` | Day 3 六项技术验收报告 |
| `reproducibility.json` | 独立复跑的指标、排名和分数比较 |
| `initial_results/` / `repeat_results/` | 保留的首轮与独立复跑原始产物；主 `results/` 使用带完整代码哈希的复跑报告 |

复跑时固定数据 manifest 和报告中的两个模型 commit，使用相同依赖及参数。
小型阅读笔记和初始标注只用于首轮检查，跨文档相关性可能标注不全；
不能据此预设 Hybrid 优于 Dense，也不能把结果视为真实业务质量结论。

独立复跑并对比（固定两套模型的 commit，复用相同环境及数据）：

```bash
OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 EMBEDDING_LOCAL_FILES_ONLY=true \
  RERANKER_LOCAL_FILES_ONLY=true HF_HUB_OFFLINE=1 \
  EMBEDDING_REVISION=5617a9f61b028005a4858fdac845db406aefb181 \
  RERANKER_REVISION=953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e \
  python -m app.evaluation.benchmark --output data/benchmark/day3/repeat_results
python -m app.evaluation.compare_runs data/benchmark/day3/results data/benchmark/day3/repeat_results
```

首轮实测（20 条 Query；初始标注待人工确认）：

| 方法 | Recall@5 | MRR（Top-10） | nDCG@10 |
| --- | ---: | ---: | ---: |
| Dense | 0.9500 | 0.9750 | 0.7859 |
| BM25 | 0.7750 | 0.9000 | 0.6195 |
| Hybrid | 0.8750 | 0.9750 | 0.7274 |
| Hybrid + Reranker | 1.0000 | 0.9417 | 0.8328 |

本轮 Hybrid 的 Recall@5 和 nDCG@10 低于 Dense；重排提高这两个指标，但 MRR 略降。
q02、q04、q11 中，关键词元数据匹配会把部分已标注答案挤出前 5。
q14 的靠前结果直接列出 TTFT/TPOT 等指标，但位于未标注的提问清单段落，存在疑似标注缺口。
这些样例保存在 `error_analysis.json`；先冻结当前标签完成比较，后续人工复核应形成新的数据版本。

## Day 4：Web Research 与 Evidence

已接入共享推理客户端、多角度 Query、Tavily、trafilatura 网页正文解析、固定缓存和
PostgreSQL `claims` / `web_sources` / `web_contents` / `web_searches` / `evidences` 表。
实际网页原文、来源和 Claim 绑定由程序核对；LLM 只提出引用片段及支持关系。

### 本地 / 远程推理分支

`app/llm_client.py` 是 Query Generation 和 Evidence Extraction 的共同入口，
后续 Planner、Router、Verifier、报告也应复用 `get_llm_client()`，不要另外创建 SDK 客户端。
FastAPI 启动时验证当前分支配置并创建客户端，关闭时释放连接。

当前本地服务已确认为 GPU 4–7 的 Qwen 服务：

```dotenv
LLM_MODE=local
LLM_LOCAL_BASE_URL=http://127.0.0.1:8010/v1
LLM_LOCAL_MODEL=/data0/liuxule/project/models/Qwen3.8-27B
LLM_LOCAL_API_KEY=
LLM_TIMEOUT_SECONDS=90
LLM_MAX_RETRIES=2
LLM_DISABLE_THINKING=true
```

本地允许 HTTP / HTTPS。免认证服务使用非秘密 SDK 占位值，客户端不读取远程密钥。
本地 Qwen 请求通过 `chat_template_kwargs.enable_thinking=false` 限制无关的思考输出；
其他本地兼容服务不支持此字段时，可设置 `LLM_DISABLE_THINKING=false`。

远程配置通过修改 `.env` 并重启 API 生效，无需改代码：

```dotenv
LLM_MODE=remote
LLM_REMOTE_BASE_URL=https://your-provider.example/v1
LLM_REMOTE_MODEL=your-actual-model-id
LLM_REMOTE_API_KEY=
```

远程分支要求 HTTPS、模型 ID 和独立的有效密钥；不会借用本地或通用密钥。
把真实密钥写入本机 `.env`，不写入示例、代码、命令输出或报告。
凭据通过 `CREDENTIALS_ENV_FILE` 指定的私有 dotenv 文件读取，默认 `.env`，不从同名环境变量取密钥。
容器部署时需只读挂载该文件；镜像不复制 `.env`。

SDK 内置重试关闭。程序仅对 429、5xx 和传输错误做有限重试，最多 `LLM_MAX_RETRIES + 1` 次调用；
鉴权、参数、无效 JSON 不重试，失败不切换后端。
`llm_trace` 记录操作名、模式、请求及实际模型 ID、耗时、次数和错误类别，不记录密钥、Prompt 或回答正文。

从 API 的真实运行环境执行最小 JSON 推理：

```bash
curl --noproxy 127.0.0.1 -fsS -X POST http://127.0.0.1:8001/api/v1/llm/probe
```

返回可校验的 Query JSON 和实际推理 Trace。本地真实调用已通过。
当前远程分支尚缺真实 HTTPS 地址、模型 ID 和远程凭据，验收状态如实保留为待配置，协议替身测试不代替远程实测。

### Tavily 与多查询生成

将 `TAVILY_API_KEY` 仅写入项目 `.env`。主客户端只读取所选分支的密钥；Tavily 也只从私有 dotenv 文件取值。
搜索设置 `SEARCH_TIMEOUT_SECONDS`，只对 429、5xx 和传输错误有限重试，
最大次数由 `SEARCH_MAX_RETRIES` 限制。每次记录 Query 和耗时，不记录凭据或异常响应正文。

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/web-research \
  -H 'Content-Type: application/json' \
  -d '{"claims":[{"claim_id":"C1","claim_text":"比较 Qwen 和 DeepSeek 的企业部署能力与 Function Calling 文档。"}],"max_queries":4,"max_results":3,"max_pages":4}'
```

只生成 Query 时调用 `POST /api/v1/web-research/queries`，请求结构相同。
每条 Query 绑定输入 Claim ID，超出上限会截断；重复搜索去除，未知 Claim ID 拒绝，
带明确命名实体的 Claim 会过滤明显偏离实体的 Query。语义相关性仍需结合实际来源审阅。
整个研究请求也限制 Query 数、每次结果数和抓取页数。
主响应包含 `status`（`complete` / `partial` / `no_evidence`）、Query、页面元数据、Evidence、Trace 和逐页错误类别。

### 网页规范化、去重与固定缓存

规范化主机名、默认端口、Query 顺序，删除 fragment、UTM、gclid、fbclid。
抓取仅接受公开 HTTP(S) 来源，并检查跳转目标；网页请求有超时、重定向次数及正文大小上限。
`trafilatura` 提取标题、正文和可获得的发布时间，抓取时间由程序填写，未知发布时间为 `null`。

`web_sources` 保存 URL、标题、时间、来源类型和搜索 Query；
`web_contents` 按正文 SHA256 去重并保存正文和缓存位置。同内容的不同 URL 仍保留独立来源记录。
缓存位于 `data/cache/web/searches`、`urls`、`contents`，研究快照位于 `research`。
内容缓存按 Hash 命名；页面和 Evidence 使用同一正文快照核对，不能用模型返回的片段替换原文。

```bash
# 固定快照回放：不重新生成 Query、不搜索、不抓新网页，也不调用 LLM。
curl --noproxy 127.0.0.1 -fsS -X POST \
  http://127.0.0.1:8001/api/v1/web-research/<run_id>/replay

# 不依赖 API 或数据库的离线证据核对，验证 Hash、来源、Claim 和逐字片段。
python -m app.evaluation.web_benchmark data/benchmark/day4/fixed_snapshot.json
```

缓存不存在时 `cache_only` 路径失败，不会静默联网。
首次真实结果已冻结到 `data/benchmark/day4`，后续 Benchmark 应使用该快照及对应正文 Hash，避免每次搜索到不同网页。

### Evidence 校验与入库

`app/evidence/schema.py` 提供严格的 Pydantic 模型，包含用户要求的全部字段，
额外记录支持关系、正文 Hash 和引用位置。
支持关系为 `supports` / `refutes` / `context`，是模型判断，需按业务需求复核。

模型输出只允许 `supporting_text` 和 `relation`，禁止生成 URL、Evidence ID 或发布时间。
程序生成确定性 Evidence UUID，绑定已存在的 Claim 和来源 URL，
核对引用必须逐字出现在提供给模型的正文中，再核对数据库中的同一 Hash 与字符位置。
伪造、改写、跨来源片段、缺失 URL 或未知 Claim 会拒绝保存。
重复快照回放不会新增相同 Evidence。

读取某 Claim 的已保存证据：

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/claims/C1/evidences
```

外部 Claim 标签按请求内唯一；相同标签但不同文本会使用不同内部 Claim 主键，
查询响应同时包含 Claim 文本以便区分。

Day 4 验收命令（真实本地 Qwen、Tavily、网页及隔离 PostgreSQL）：

```bash
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python scripts/acceptance_day4.py
```

验收报告位于 `data/benchmark/day4/acceptance.json`；当前本地流程六项通过，
远程实测因缺少配置保持 `partial`。配置远程分支后重跑可执行两种模式的真实 API Probe，
不会把 HTTPS 协议模拟测试当作远程实测通过。

## LangGraph、Agent State 与 Tool Calling

任务执行使用真实 LangGraph `StateGraph`，六个节点为：

```mermaid
flowchart TD
    S[START] --> P[planner]
    P --> R[router]
    R --> T[tool_executor]
    T --> U[state_updater]
    U --> V[verifier]
    V -->|需要更多证据且有预算| R
    V -->|覆盖足够或预算耗尽| O[reporter]
    P -->|规划失败| O
    O --> E[END]
```

### State 和持久化

`app/agent/state.py` 定义用户要求的任务、Claims、Evidence ID、动作、当前 Claim、
搜索/检索次数、剩余预算、报告与状态等字段，并补充轮数、来源 ID、短 Observation 和停止原因。
完整网页和文档快照保存在数据库，不进入 State。
最近 12 条步骤摘要最多各 400 字符，State JSON 总大小限制为 64 KiB。
最终报告可以包含有限的原文节选和引用 ID，不能把网页全文当作 Observation 返回给 Router。

每个节点将状态写入 `agent_runs`，每次工具尝试写入 `agent_steps`。
任务启动通过数据库状态更新认领，避免并发重复执行。
已结束任务再次运行返回原结果，不重新调用工具；正在运行的任务返回 `409`。
服务重启后可读取已有 State 和步骤，并可从数据库快照手动恢复。未确认完成的工具调用不会被盲目重放。

### Planner 和 Router

Pydantic 模型包含 `ResearchPlan`、`Claim`、`Priority`（high/medium/low）和
`RequiredSource`（official/academic/internal/any）。Claim ID 必须唯一，规划最多 6 个 Claim。
规划将复杂问题拆成中性研究问题，保留比较对象，不预设厂商能力，也不添加未请求的评测维度。

LLM JSON 或 Schema 无效时，将安全的错误位置与类型反馈给同一客户端，最多修复一次；
再次失败则任务为 `failed`。网络层仍遵循 `LLM_MAX_RETRIES` 的有限重试规则，不切换后端。

当前工具调用使用 **结构化 Action 兼容模式**：模型返回动作、Claim ID 和参数，
不是模型自行执行 Python，也不把它宣称为原生 `tool_calls`。
Router 和 Executor 都使用同一组 Pydantic 工具 Schema，之后还会检查白名单、当前 Claim、来源权限和预算。
本地与远程分支复用 `app/llm_client.py`。本地真实运行已验收；远程 HTTPS 的协议替身测试覆盖了完整动作执行，
真实远程服务仍待有效配置，不以模拟测试替代远程实测。

### 四个工具

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `web_search_tool` | `query` | Tavily 搜索最多 2 条结果，并解析、缓存可用页面；授权给当前任务 |
| `rag_retrieve_tool` | `query`, `top_k=5`（1–10） | 从已有 Dense/Hybrid 检索器召回，保存带 document_id、chunk_id 的原文 Evidence |
| `open_source_tool` | `url` | 仅读取当前任务此前搜索并缓存的来源，抽取和核对逐字 Evidence |
| `verify_claim_tool` | `claim_id` | 核对原文、Hash、来源、时效，通过共享 LLM 显式判断整条 Claim 是否有充分依据 |

未知工具、额外参数、越界 top_k、未知 Claim 均拒绝。URL 只允许 HTTP/HTTPS，
拒绝 localhost、内网/链路本地 IP 和云元数据地址。
`open_source_tool` 必须命中 `agent_allowed_sources` 中当前任务的授权来源，
不能打开仅由模型编造的 URL，也不能借用其他任务的缓存授权；该工具本身不发起目标网页 GET。

RAG Evidence 使用 `source_type=document`，允许 `source_url=null`，但绑定真实 Document/Chunk；
Web Evidence 仍要求已存在的 URL。二者均核对不可变正文快照和引用位置。
现有 `web_contents` 表兼作原文快照存储；启动时为 `evidences` 增加 `chunk_id` 并放宽文档 Evidence 的 URL 列，
原有 Web Evidence 保留，应用层继续强制每条 Web Evidence 有 URL。

Router 先校验动作与参数；非法提议记录到 `agent_policy_decisions` 并弃答，不派发工具、不消耗工具预算。
通过校验的工具尝试先预留一个预算单位并记录步骤；空结果、失败和超时均消耗预算。
预算不足不派发工具。API 预算范围为 1–20，运行时再受 `AGENT_MAX_STEPS=12` 限制。
工具使用独立工作线程和独立 SQL Session，外层等待有截止时间；超时会停止该任务的新动作。
Python 不能强制杀死工作线程，因此网络阶段使用剩余超时，持久化前再次检查取消/截止时间，
迟到结果不会更新 Agent State。未配置的全局任意 Python 工具不能注册执行。

```dotenv
AGENT_TOOL_TIMEOUT_SECONDS=30
AGENT_MAX_STEPS=12
AGENT_MAX_RETRIEVAL_ACTIONS=6
AGENT_MAX_CLAIM_RETRIEVALS=3
AGENT_MAX_QUERY_REWRITES=2
AGENT_EVIDENCE_MAX_AGE_DAYS=365
AGENT_RAG_METHOD=hybrid
```

### Verifier 和报告

Verifier 检查 Evidence 是否属于当前 Claim、正文 Hash 与引用位置是否一致，
以及来源和时效是否满足要求。第一版通过固定域名/路径表识别官方、学术来源，
RAG 文档视为内部资料，不因文件名含“官方”就认定为官方依据。
引用合法和来源合格只产生 `RETRIEVED`，不能直接产生 `SUPPORTED`。
显式 VERIFY 通过共享推理客户端判断整条 Claim；程序核对其引用 ID 和来源要求后保存验证记录。
模型验证仍可能出错，报告保留原文和依据，便于人工复核。
报告展示原文、来源、Evidence ID 和仍缺少的依据。
预算耗尽或工具超时时返回带缺口说明的 `partial` 报告，规划失败返回 `failed`，不把任务强行写成成功。

### API 用法

先沿用现有任务创建接口，拿到 `id`：

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/research \
  -H 'Content-Type: application/json' \
  -d '{"query":"比较 Qwen 和 DeepSeek 的企业私有部署与 Function Calling。先查本地知识库，再核对官方文档。","budget":8}'
```

创建后由 BackgroundTasks 自动执行，通过查询接口跟踪：

```bash
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/tasks/<task_id>/agent
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/tasks/<task_id>/steps
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/api/v1/tasks/<task_id>/report
```

兼容接口 `POST /tasks/{task_id}/run` 为同步执行；运行中的后台任务返回 409，已完成任务返回原结果。
最终报告位于 `state.final_report`，也可通过 `/report` 获取。
`steps` 返回工具名、Schema 校验后的参数、状态、结构化 Observation、耗时、错误类别和模型 Trace，
不包含网页全文、SDK 密钥或完整模型 Prompt。

### 真实验收

```bash
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python scripts/acceptance_agent.py
```

脚本使用本地真实 Qwen、BGE RAG、Tavily 和隔离 PostgreSQL，检查复杂 Claims、Web/RAG 调用、
参数 Schema、全部 `agent_steps`、至少两轮 Action–Observation，以及重启持久化和重复运行幂等性。
产物位于 `data/benchmark/agent/acceptance.json` 和 `run.json`。
六项当天验收与两种推理模式的实测状态分别记录；远程配置缺失时总状态保持 `partial`。

## Adaptive Evidence Acquisition

新任务采用 `adaptive-v1`：Planner → Evidence State → LLM 提议 → Policy 校验 → 工具执行
→ Evidence 更新 → Coverage 重算 → 继续 / STOP / ABSTAIN。沿用已有任务 API，无需新增调用方式。
已有任务的历史 State 和报告保持可读取；新算法仅用于新建任务，不重放旧任务。

### Evidence State 与 Coverage

| 状态 | 含义 |
| --- | --- |
| `NOT_RETRIEVED` | 尚未采集到当前 Claim 的可追溯证据 |
| `RETRIEVED` | 有新鲜原文且来源满足要求，等待显式语义验证 |
| `SUPPORTED` | 整条 Claim 验证通过，支持引用满足来源、时效要求，无未解决冲突 |
| `CONTRADICTED` | 存在相反关系的片段待核对，或显式验证识别出反证 |
| `INSUFFICIENT` | 已尝试采集但无有效证据、来源不合格，或验证认为依据不足 |
| `OUTDATED` | 有可追溯片段，但均超出时效窗口 |

优先级由程序映射为权重 high=3、medium=2、low=1。
`calculate_coverage` 只将 `SUPPORTED && verified && source_requirements_met && fresh && !has_conflict`
计入分子；分母为全部 Claim 权重之和，空集合或总权重为零返回 0。
仅标注 `status=SUPPORTED` 或片段关系为 `supports` 都不能计入 Coverage。

默认时效窗口 365 天：优先使用发布时间，缺失时使用抓取时间；超前超过一天的时间戳也视为不可用。
这是一条保守的可配置策略，抓取时间无法证明页面内容刚刚更新。
官方、学术来源采用程序域名/路径表；需要更多来源时扩充 `source_class`。
Claim 可设置 `needs_internal` / `needs_external`，要求同时满足内部与外部依据。

显式 VERIFY 复用 `app/llm_client.py`，要求模型返回 verdict、支持/反对 Evidence ID 和理由。
程序核对引用必须来自本次输入；SUPPORTED 的支持引用自身必须满足所有来源要求。
结果和安全 Trace 存入 `evidence_assessments`，按任务、Claim 和证据指纹隔离。
指纹包含 Claim、来源分类、正文 Hash、关系、日期和新鲜度；证据变化或过期后旧验证不再覆盖新状态。
同一证据集不重复验证。非法引用或推理失败会记录错误并 ABSTAIN。

### 动作、规则与预算

模型输出：

```json
{"action":"SEARCH_WEB","target_claim_id":"C1","reason":"缺少官方依据","arguments":{"query":"Qwen official deployment documentation"}}
```

白名单为 `SEARCH_WEB`、`RETRIEVE_RAG`、`OPEN_SOURCE`、`VERIFY`、`STOP`、`ABSTAIN`。
`app/agent/policy.py` 独立于模型执行校验，Executor 派发前再次校验：

- 关键 Claim 全部通过验证时 STOP；没有 high 优先级时要求全部 Claim 通过。
- STOP 仍保留未完成的非关键 Claim 缺口，此时报告为 partial，Coverage 可以小于 100%。
- 预算耗尽、达到最大步数、非法提议或验证不可用时 ABSTAIN，不再调用工具。
- 未核对的冲突优先 VERIFY；有满足来源要求的新片段时先 VERIFY。
- 缺内部依据时选 RAG，缺外部依据时选合格缓存来源或 Web Search。
- 证据不足时继续采集；单 Claim 或全任务检索达到上限且没有可验证的新依据时 ABSTAIN。

| 限制 | 默认值与计数方式 |
| --- | --- |
| 工具总预算 | API budget 默认 6；每次已派发尝试减 1，失败也计入；有效值不超过 12 |
| 全任务检索上限 | SEARCH_WEB + RETRIEVE_RAG 合计最多 6 次 |
| 单 Claim 检索上限 | SEARCH_WEB + RETRIEVE_RAG 合计最多 3 次 |
| Query Rewrite | 全任务最多 2 次；同 Claim 再次使用同一检索器算一次改写，即使模型主动换 Query |
| 工具超时 | 默认 30 秒，可调低；超时终止后续动作 |
| Agent Step | 最多 12 次动作决策；STOP/ABSTAIN 不派发工具、不扣工具预算 |

各上限可通过上方 `AGENT_*` 配置调低，不能调高到超过本版硬上限。
重复规范化 Query 必须改写且不得再次重复；改写失败或次数用尽立即 ABSTAIN。
OPEN_SOURCE 和 VERIFY 消耗工具预算与步数，不计入检索次数；缓存来源每个 Claim 最多打开一次。
冲突验证后仍有分歧时可继续在剩余检索预算内找新依据，不会用同一证据无限 VERIFY。

`agent_policy_decisions` 保存模型提议、实际动作、覆盖理由和计数快照；
`agent_steps.trace.policy` 保存对应动作的规则依据。
State 新增 coverage、coverage_history、termination_action、claim_statuses 和各类预算计数，完整正文继续保存在数据库。

### 复现实测

```bash
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python scripts/acceptance_adaptive.py
```

脚本使用独立 PostgreSQL Schema，结束后清理 Schema，不修改已有业务文档。
六项验收的冲突、耗尽等边界场景使用明确标记的模型/采集替身，执行真实 LangGraph、Schema 校验、
Evidence 持久化和语义验证记录流程；另外通过真实 FastAPI、当前本地 Qwen、BGE-M3 和 Tavily
执行内部知识库与官方网页两次研究任务，检查服务重启和重复运行幂等性。
真实网页依据不足时允许 ABSTAIN，不能为了验收把它改写成已验证。

产物：`data/benchmark/adaptive/acceptance.json`、`scenarios.json`、`run.json`。
报告区分受控边界场景与真实推理结果；真实推理 Trace 包含实际模型 ID，原始密钥和敏感 Prompt 不写入报告。

## Claim Verification、Memory、Recovery 与后台 API

验证结果保存于 PostgreSQL `evidence_assessments`。结果包含 claim_id、四态 status
（SUPPORTED / CONTRADICTED / INSUFFICIENT / OUTDATED）、supporting_evidence_ids、
contradicting_evidence_ids、逐条 evidence_relations、原文片段和 reason。
程序拒绝输入之外的 Evidence ID，并再次检查来源、Hash、引用位置、范围和时效。
没有直接支持引用时不能产生 SUPPORTED。检索阶段的 NOT_RETRIEVED / RETRIEVED 仍保留为采集状态。

Evidence 可携带 `scope={model_name,model_version,deployment_mode}`；未知值保留 null。
`detect_contradiction` 先比较型号、模型版本、部署方式和发布日期。数字不同只能形成候选，
不同型号、API/本地部署、版本差异不会直接判为冲突；不同发布日期可能反映更新。
同范围候选也需显式语义验证才能确认为 CONTRADICTED；范围不明时使用 INSUFFICIENT。
`detect_outdated` 同时检查日期和调用方明确提供的目标模型版本。
文档修订版本不会被自动当作模型版本。来源中明确的范围元数据优先于模型推断。

Reflection 将上一动作错误、来源缺口和过期状态归为失败原因，再改写查询。
分析、原查询、新查询和安全模型 Trace 保存到 `agent_reflections`。
它与全任务最多 2 次 Query Rewrite 共用计数，没有额外的无限重试循环。

| Memory | 实现 |
| --- | --- |
| Short-term | 当前 LangGraph State |
| Long-term | PostgreSQL research_tasks、agent_runs 中的任务与报告 |
| Execution | agent_steps、agent_policy_decisions、agent_reflections |
| Checkpoint | state_snapshots：状态、序号、下一执行节点 |

每个节点的状态、任务状态和下一节点在同一事务中提交。
派发工具前，预算预留、工具步骤与恢复检查点也在同一事务中提交。
RAG、网页引文和验证工具将 `tool_execution_receipts` 与最终数据库结果一起提交；
通用工具也写执行回执，但中途已提交的外部动作可能存在未知窗口。

手动恢复 `POST /api/v1/tasks/{task_id}/recover` 读取最新快照：

- 已有结果或执行回执：恢复 Observation 并继续更新 State，不重复调用成功工具。
- 已在 State 应用的步骤按 step_index 去重，计数不会重复增长。
- 派发前中断：保留已预留预算，记录该次未执行，再由策略决定后续动作。
- 已派发但无可信结果/回执：标记 unknown，ABSTAIN 并保留待核对说明，不盲目重试副作用。
- 未消耗的预算、Claim、Evidence ID、检索次数和改写次数全部沿用，不因恢复而重置。

任务使用数据库 worker lease 防止重复恢复。`AGENT_LEASE_SECONDS=360`，实际租约至少覆盖
一次推理的有限重试窗口加 60 秒；每次检查点和工具提交续约。
有效租约期间恢复返回 409；进程崩溃后等待租约失效再手动恢复。
持久化前检查 worker token，旧 worker 不能覆盖新 worker 的状态或提交迟到工具结果。
旧版本未产生 `state_snapshots` 的历史任务可查询，但不能凭空恢复。

BackgroundTasks 是同进程本地 MVP，不是可靠任务队列。进程退出后任务不会自动重启，
这里提供数据库检查点与手动入口，不承诺生产级分布式 exactly-once 执行。

| Method | Endpoint | 行为 |
| --- | --- | --- |
| POST | `/api/v1/research` | 校验后创建并提交任务，返回 id/task_id，BackgroundTasks 用独立 Session 执行 |
| GET | `/api/v1/tasks` | 分页历史任务 |
| GET | `/api/v1/tasks/{id}` | 任务状态 |
| GET | `/api/v1/tasks/{id}/trace` | 工具步骤、策略、Reflection 和快照元数据 |
| GET | `/api/v1/tasks/{id}/report` | 报告；未生成时 202，生成后 200 |
| GET | `/api/v1/tasks/{id}/verifications` | 已保存的逐 Claim 验证历史 |
| POST | `/api/v1/tasks/{id}/recover` | 202 调度手动恢复；活跃或已结束任务返回 409 |
| POST | `/api/v1/documents/upload` | 文档上传；原 `/documents` 上传接口仍可用 |
| GET | `/api/v1/documents` | 分页文档列表 |
| POST | `/api/v1/tasks/{id}/feedback` | 保存 rating=1–5 及可选 comment |
| GET | `/api/v1/evaluations` | 从 PostgreSQL 查询启动时导入的评测报告 |
| GET | `/health` | 数据库探活，异常返回 503 |

列表使用 limit=20、offset=0，最大 limit=100。未知任务返回 404，非法 UUID/参数返回 422。
实时轨迹不返回全文网页或原始模型 Prompt。反馈是附加记录，不自动修改 Ground Truth。

```bash
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python scripts/acceptance_memory_api.py
```

验收产物位于 `data/benchmark/memory_api/`。恢复测试通过真实工具回执构造中断窗口，
重启真实服务后恢复；报告明确标记这一受控中断，不将它冒充随机生产崩溃实测。

## 研究任务 Benchmark、Baseline 与 Ablation

研究任务级数据集位于 `data/benchmark/research_v1/`，与 Day 3 的检索排名指标分开。
固定 20 个任务：单来源 4、多来源 4、多跳 4、来源冲突 3、过期信息 3、无法回答 2。
每项均有 task_id、query、required_claims、reference_evidence、expected_status 和 evaluation_rubric。
required_claims 为中性研究问题，答案模式、参考陈述与引文标注只供评分器使用，不能发给被测模型。

资料取自 BAAI、Qwen、pgvector、FastAPI、LangGraph、Pydantic 和 PostgreSQL 官方站点/仓库。
`sources.json` 保存完整正文、原始/正文 Hash、URL、日期、范围和检索通道；原始响应位于 `raw/`。
未知发布时间不从版权年份或新闻列表推断。历史版本日期保留官方页面/提交元数据的出处。
`chunks.json` 固定 500/80 切分结果，`manifest.json` 校验全部任务、正文与 Chunk 文件 Hash。
评测按固定 evaluation_as_of 检查时效，不随运行日期悄悄改变标签。

当前标注为 `agent_reviewed_pending_human`、`human_reviewed=false`。
参考规则由独立资料核对与固定代码编写，不通过被测模型生成唯一标准答案，也不使用被测模型作唯一评分者。
仍需人工逐条确认官方原文、语义覆盖与评分规则；`human_review.csv` 是复核工作表。
人工复核前的结果仅为初步实验，验收报告会将正式 Ground Truth 条件保留为待完成，不能声称已证明普遍优于 Baseline。
已经实测的数据集不能直接运行标注生成脚本覆盖；更改标注/快照应创建新版本。
本轮冲突组重点检查不同型号、索引类型和版本造成的表面冲突，不能单凭这些任务估计
“同范围、独立官方来源之间不可消解矛盾”的检出率；正式扩大 Benchmark 时应补充该类正例。

| 方法 | 执行流程 |
| --- | --- |
| Naive RAG | Dense → Top-5 → 同一模型生成答案 |
| Fixed Search + RAG | 固定执行缓存 Web Search 和 Dense RAG → 同一模型生成答案 |
| ReAct | 同一模型循环选择 Search/RAG/Open/Finish，读取 Observation，不维护显式 Coverage/Verifier |
| EvidencePilot | 现有生产 LangGraph：拆解、Evidence State、自适应规则、显式验证与停止/弃答 |
| w/o Evidence State | Router 隐去状态与 Coverage，取消证据驱动的采集路由，保留硬预算与独立验证 |
| w/o Claim Verification | 在隔离评测库内用来源存在性替代语义验证，作为明确标记的缺陷消融 |
| w/o Stopping Policy | 取消证据充分时的提前停止，仍保留预算、步数、单 Claim 和改写硬上限 |

消融策略仅由 Python 实验入口注入，API 请求不能选择它们或绕过生产规则。
全部方案共享同一模式、同一客户端和实际模型、同一任务与源集合、最大 6 次检索预算、Top-5、
最多 10 个最终上下文 Chunk。Ours 的工具总预算为 12（包含 Open/Verify），单 Claim 最多 3 次检索。
固定流程的实际调用数可以少于预算，其成本优势直接计入指标，不强迫它消耗剩余预算。

缓存 Web Search 使用固定官方网页窗口的 BM25，不再次调用 Tavily；因此结果评价缓存资料上的
研究流程，不能用来声称线上搜索引擎质量。Dense 使用真实 BGE-M3 和 PostgreSQL 精确余弦排序。
每个任务通过固定 `allowed_source_ids` 限制候选来源；这是预选资料上的受控比较，
不能据此评价开放互联网中的来源发现能力。
RAG 语料由同一批官方网页快照构成，保存并验证原始官方 URL；不会把任意上传文件名当成官方来源。
Open 只能打开当前任务搜索授权的缓存 URL，并从命中窗口抽取逐字片段。

所有方案使用同一版本最终答案 Schema/Prompt，Ours 额外约束确定回答只能引用当前显式验证通过的证据。
原文、模型生成文本、动作、耗时和 Trace 全部保存。运行顺序用固定 seed=0 打乱，以减轻方法固定先后顺序的偏差。
temperature=0，记录实际模型 ID、服务版本/指纹、可获得的本地模型配置 Hash、权重文件清单、
Embedding 提交版本、软件版本、Prompt Hash 和实现文件 Hash。无法获得的版本或 token 用量明确记为未知。
复用现有推理服务，无法控制其他用户的流量，因此延迟只是该轮实测，不是吞吐或性能保证。

自动评分不调用模型，使用固定引文相关性标注和答案模式：

| 指标 | 定义 |
| --- | --- |
| Answer Accuracy | 全部必答 Claim 的状态、答案模式和必要证据组均通过的任务比例；额外断言不忽略 |
| Claim Accuracy | 必答 Claim 的规则准确率，作为辅助指标 |
| Evidence Coverage | 有合格、精确引用证据组的必答 Claim 数 / 全部必答 Claim；无法回答任务不虚增覆盖 |
| Citation Precision | 来自实际已检索 Chunk 的逐字片段，且匹配独立标注原文的引用数 / 全部引用数 |
| Unsupported Claim Rate | 无足够标注证据或答案事实不合格的确定断言数 / 全部确定断言数 |
| Retrieval Calls | Search + RAG 实际调用次数均值，包括空结果和重复查询 |
| Token Cost | API 返回的 total_tokens 均值；包括规划、路由、抽取、验证和答案，不推算费用 |
| Latency | 任务端到端秒数，含查询向量、数据库和推理，不含语料向量预计算 |

没有引用或没有确定断言时，对应比例为 null，不人为记作满分。
只要某个任务缺失 API token 用量，方案均值就保持 null，同时报告可获取用量的任务数。
每条记录保留分子/分母、逐 Claim 评分、错误和证据 ID，支持人工审计。
规则检查仍可能漏掉语义问题，需与人工复核结合。

```bash
# 官方资料获取仅在建数据集时运行一次；已冻结时拒绝自动刷新。
python scripts/prepare_research_benchmark.py
python scripts/annotate_research_benchmark.py

# 同一模式跑全套 20 × 7 实验；模式由当前 .env 选择。
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  HF_HUB_OFFLINE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 \
  python -m app.evaluation.research_benchmark

# 无网络、无推理调用重放已保存输出的自动评分。
python -m app.evaluation.research_benchmark --replay

# 仅在数据、代码、参数、模型及服务指纹都相同时续跑未完成任务。
EVIDENCEPILOT_TEST_POSTGRES_URL=postgresql+psycopg://user:password@localhost:5432/test_db \
  python -m app.evaluation.research_benchmark --resume

# 核对任务/模型/代码身份、全部预算和 token 账目，再生成配对比较与失败分析。
python scripts/audit_research_run.py
python scripts/analyze_research_results.py
```

每轮输出路径必须为空，避免把不同模型、Prompt 或标签的记录混合。
同版本恢复使用 `--resume`，已得到的结果（含模型 Schema 失败）不会挑选性重跑。
服务不可用或实际推理指纹改变时立即中断，未完成尝试写入 `interrupted_attempts.jsonl`，
不将后续批量连接失败当作算法表现。服务恢复为原指纹后可续跑；指纹变化则新建完整实验批次，不能拼接比较。
只需冒烟时可用 `--task-ids research_001 research_013 --output <new_directory>`。
`results/` 包含 `results.json`、`results.csv`、逐条 `runs.jsonl`、五个实际失败记录的 `bad_cases.json`、
`replay.json` 和 `acceptance.json`。固定输入可重新推理，也可重放保存的推理输出；后者保证评分可重现，
不宣称共享 GPU 上的实时生成必然逐字相同。

2026-11-20 完整实测：20 个任务 × 7 种方案，共 140 条记录。全部使用 local 模式、
`/data0/liuxule/project/models/Qwen3.8-27B`，服务指纹为 `vllm-0.23.0-tp4-ad065b3e`。
下表是冻结初始标注的规则评分，仍待人工复核。

| 方法 | 准确率 | 证据覆盖率 | 引用精确率 | 无支持断言比例 | 平均检索次数 | 平均耗时（秒） | 平均 Token |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Naive RAG | 35.0% | 19.4% | 24.3% | 66.7% | 1.00 | 11.8 | 3567.6 |
| Fixed Search + RAG | 40.0% | 32.3% | 29.8% | 65.4% | 2.00 | 15.9 | 5183.4 |
| ReAct | 45.0% | 32.3% | 34.1% | 64.0% | 1.05 | 17.9 | 7300.8 |
| EvidencePilot | 45.0% | 38.7% | 45.5% | 52.6% | 2.05 | 63.3 | 未知（19/20 可用） |
| w/o Evidence State | 25.0% | 12.9% | 53.8% | — | 0.80 | 15.2 | 4606.6 |
| w/o Claim Verification | 40.0% | 35.5% | 44.4% | 61.1% | 1.65 | 32.6 | 14694.3 |
| w/o Stopping Policy | 45.0% | 35.5% | 41.0% | 61.9% | 3.00 | 60.7 | 未知（19/20 可用） |

EvidencePilot 与 ReAct 的准确率持平，覆盖率更高、耗时也更高；相对三个 Baseline 的准确率差异
在本任务集的配对 Bootstrap 95% 区间均包含 0，不能据此宣称已证明整体优势。
无 Evidence State 的方案没有确定断言，对应比例未定义，不记作零风险。
全部 31 条必答 Claim 都进入覆盖率分母，其中 5 条要求识别过期或无法回答；参考最大覆盖率为 26/31。
本轮保留 1 次 ReAct JSON 格式失败和 2 次验证工具超时；超时任务缺失用量，因此相关方案不计算不完整的 Token 均值。

`results/comparison.csv` 保存完整指标；`analysis.json` 保存配对比较、执行失败计数和解释边界；
`bad_cases.json` 分析五个不同任务的实际失败，`failure_catalog.json` 保留全部规则评分失败。
真实引文落在初始标注范围外、答案同义表达未匹配等情况明确标为待人工复核，不自动当作事实错误。
`integrity_audit.json` 确认 140 条记录的代码、模型、预算和评分回放一致；回归测试为 166 passed、0 failed、0 skipped，含真实 PostgreSQL/pgvector。
工程验收通过，正式 Ground Truth 验收仍为 `pending_human_review`。

## Streamlit 与最终演示

| 页面 | 功能 |
| --- | --- |
| Research Workspace | 提交研究任务、预算设置、每 2 秒轮询状态、打开历史任务 |
| Document Upload | 上传 PDF/DOCX/Markdown，设置 500/80 等分块参数，查看 Chunk 与向量维度 |
| Agent Trace | 查看参数、Observation、错误类别、模式及实际模型 ID、策略、反思、恢复快照 |
| Evidence Explorer | 按当前任务查看 Claim 与 Evidence；点击 ID 核对原文、来源、位置和 Hash |
| Research Report | 展示最终报告、证据索引、下载 Markdown、保存反馈 |
| Evaluation Dashboard | 从 API 读取冻结实验表、图表和模型配置，下载 CSV |

前端仅依赖 [frontend/api_client.py](frontend/api_client.py) 的 HTTP 接口，不访问数据库。
页面链接保留 `?task=<UUID>`，浏览器刷新后可以重新读取历史任务。
自动跟踪 15 分钟后暂停，可手动继续；关闭浏览器不会取消后端任务。
任务/上传写请求不自动重试：响应丢失时应先查看历史任务或文档列表，避免重复写入。

原文查询接口：

```text
GET /api/v1/tasks/{id}/evidences
GET /api/v1/tasks/{id}/evidences/{evidence_id}
GET /api/v1/system
```

前两项仅返回当前任务所引用的 Evidence。详情返回保存的网页正文或文档 Chunk，
不会重新抓取任意 URL；Hash 不符时不会把变更后的正文展示为可信原文。
上传的原始二进制文件不长期保留，PDF 页码与解析后的 Chunk 可查。
`/system` 只返回当前模式、模型 ID 和版本，不返回凭据。

### Conda 与混合部署（已验证的可选方式）

已验证 PostgreSQL 在 Docker、FastAPI/Streamlit 在 Conda 的混合方式。
随后也构建并启动了完整三服务 Compose，API 容器内 BGE-M3 实测输出 1024 维。
Qwen 服务均独立运行；训练不在应用镜像启动流程中执行。

```bash
conda create -n evidencepilot python=3.12 -y
conda activate evidencepilot
python -m pip install -r requirements.txt
```

已有环境可以直接激活。首次配置时按 `.env.example` 创建私有 `.env`；已有 `.env` 不要覆盖。
数据库连接、当前模式地址与模型填写在 `.env`，Key 只保存在这个私有文件中。

```bash
docker compose up -d --wait postgres
python scripts/dev_stack.py start
python scripts/dev_stack.py status
```

默认 API 为 `http://127.0.0.1:8001`，界面为 `http://127.0.0.1:8501`。
VS Code Remote SSH / SSH 用户需转发相应端口。启动器只管理自己记录且创建身份匹配的进程。

```bash
python scripts/dev_stack.py stop
docker compose stop postgres
```

混合方式的隔离验收使用以下端口，保留原数据库及其他应用：

```bash
POSTGRES_PORT=15432 docker compose -p evidencepilot-final up -d --wait postgres
DATABASE_URL=postgresql+psycopg://evidencepilot:change_this_password@127.0.0.1:15432/evidencepilot \
  EMBEDDING_LOCAL_FILES_ONLY=true HF_HUB_OFFLINE=1 \
  python scripts/dev_stack.py start --api-port 18001 --ui-port 18501
python scripts/dev_stack.py status --api-port 18001 --ui-port 18501
```

混合验收界面为 `http://127.0.0.1:18501`，API 为 `http://127.0.0.1:18001/docs`；
最终演示使用下面的 Compose 端口 `18502/18002`。
示例数据库密码仅供本地开发，实际连接信息应与自己的数据库配置一致。
API/Streamlit 日志位于 `data/cache/dev/<api-port>-<ui-port>/`。

### 三服务 Compose 配置

`docker-compose.yml` 包含 PostgreSQL、FastAPI、Streamlit，应用服务使用 `apps` profile。
Dockerfile 分离轻量前端和 CPU 推理后端，前端镜像不包含后端模型和训练依赖。

```bash
docker compose --profile apps config --quiet
docker compose --profile apps up -d --build --wait
docker compose --profile apps ps
```

本轮前端与 CPU API 镜像均已成功构建，三个服务均通过健康检查。
实际演示配置位于 `deploy/demo.env`，没有模型或 Tavily Key；密钥仍只来自私有 `.env`。
它使用独立的 Compose 项目、PostgreSQL 命名卷和演示数据库，保留原数据库。

```bash
docker compose --env-file .env --env-file deploy/demo.env -p evidencepilot-final --profile apps up -d --build --wait
docker compose --env-file .env --env-file deploy/demo.env -p evidencepilot-final --profile apps ps
```

最终界面 `http://127.0.0.1:18502`，API `http://127.0.0.1:18002/docs`，数据库本机端口 `15432`。
`deploy/demo.env` 中的 `LLM_CONTAINER_LOCAL_BASE_URL` 是本机已验证的宿主机地址，
其他机器应改为其容器可达地址或同网络服务名。它不覆盖 `LLM_MODE`，模式仍由私有 `.env` 选择。
此配置启用 BGE 离线缓存，首次使用前需按本页前文准备 `data/cache/models`。
自定义数据库或带特殊字符的连接密码可通过 `COMPOSE_DATABASE_URL` 提供完整、正确编码的容器 DSN。

先从 API 容器执行 `/api/v1/llm/probe` 验证模型实际可达，再运行端到端研究；健康检查本身只确认应用与数据库。
数据库使用命名卷持久化；API 的 BGE、网页缓存使用持久挂载，Benchmark 只读挂载。
私有 `.env` 仅挂载给 FastAPI，由 `CREDENTIALS_ENV_FILE` 指定；不会传给 Streamlit。

一般配置中容器内的本地模型地址使用 `LLM_CONTAINER_LOCAL_BASE_URL`，默认
`http://host.docker.internal:8010/v1`，不能填写容器自身的 `127.0.0.1`。
若模型服务与 API 位于同一网络，可使用模型服务名。
若现有 vLLM 只监听宿主机回环地址，应采用混合部署，或自行提供容器可达的服务地址；
本项目不会修改已运行模型的监听范围。
远程模式不需要启动模型容器。

### 模式切换与故障排查

1. 在私有 `.env` 设置 `LLM_MODE=local`，填写 `LLM_LOCAL_BASE_URL` 和实际 `LLM_LOCAL_MODEL`。
2. 远程模式改为 `LLM_MODE=remote`，填写 HTTPS 的 `LLM_REMOTE_BASE_URL`、`LLM_REMOTE_MODEL` 和自己的 `LLM_REMOTE_API_KEY`。
3. Conda 方式停止再启动 API，使共享客户端重建；Streamlit 无需读取或保存 Key。
   Compose 方式要重新创建 API 容器以重新注入环境变量，仅执行 `restart` 不会更新 Compose 配置。
4. 先调用 `POST /api/v1/llm/probe`，再提交同一非敏感任务：根据内部文档核对默认 Chunk Size 和 Overlap。
5. 检查 Trace 的 mode、actual_model、工具 Observation、报告与 Evidence 原文；配置/鉴权失败不会切换后端。

演示配置下更新模式、模型或地址后执行：

```bash
docker compose --env-file .env --env-file deploy/demo.env -p evidencepilot-final --profile apps up -d --force-recreate --wait api
```

| 现象 | 检查与行为 |
| --- | --- |
| API 不能启动 | 当前模式地址、模型或 Key 缺失会在启动校验中报错；未选分支不会兜底 |
| local 不可达 | 检查 `/v1`、端口、服务监听范围；有界重试后记录 transport |
| remote Key 无效 | 返回 authentication，401/403 不重试，不调用 local |
| 数据库断连 | API 返回脱敏 503，界面显示依赖不可用；恢复数据库后可读取历史 |
| Tavily 超时 / 无检索结果 | 保留结构化错误、空结果、预算和证据缺口，不伪造答案 |
| 模型 JSON 非法 | Planner 只修复一次；仍失败则保存 failed，不无限调用 |
| 预算耗尽 / 无足够证据 | STOP/ABSTAIN 受规则限制，报告保留未验证部分 |
| 进程中断 | 使用任务 Trace 与手动恢复入口；已提交回执不会被盲目重放 |

真实远程地址、模型和 Key 当前未配置，用户选择保留真实远程端到端验收为待完成。
协议替身测试覆盖 HTTPS、鉴权、工具参数与不跨模式转发，但不能替代真实远程服务实测。

### 浏览器验收与视频

`scripts/browser_acceptance.py` 使用真实 Chromium 操作六个页面，执行上传、研究、轮询、
原文弹窗、报告下载、评测与浏览器刷新后的历史查询。它不替换模型、Embedding 或数据库响应。

```bash
python -m pip install playwright
python -m playwright install chromium ffmpeg
python scripts/browser_acceptance.py
python scripts/browser_acceptance.py --api http://127.0.0.1:18002 --ui http://127.0.0.1:18502 \
  --record --seconds 220 \
  --output data/benchmark/final_acceptance/video
```

`scripts/acceptance_deployment.py` 在专用 `evidencepilot-final` 项目上短暂停止数据库并重启应用，
检查真实 503、界面诊断、数据卷持久化及历史报告/Trace/Evidence 不变；运行时保持演示任务已结束。
`scripts/acceptance_memory_api.py --output data/benchmark/final_acceptance/memory_api` 验证手动快照恢复，
其中中断窗口由真实工具回执构造，不能等同于生产分布式故障恢复。

演示视频：[完整研究流程（约 3 分 40 秒，无旁白）](docs/demo/evidencepilot-demo.webm)。
录制文件实测时长为 220.04 秒，包含真实模型请求与界面操作；未使用模拟模型或预填报告替代执行。
截图：[研究完成](docs/demo/research-completed.png)、[Trace](docs/demo/trace.png)、
[原始证据](docs/demo/evidence.png)、[报告](docs/demo/report.png)、[评测看板](docs/demo/evaluations.png)。

![EvidencePilot 研究工作区](docs/demo/research-completed.png)

![EvidencePilot 原始证据](docs/demo/evidence.png)

回归产物保存在 `data/benchmark/final_acceptance/`。
最终本地工程验收通过：完整 Compose 的真实浏览器流程、PostgreSQL 断连诊断、
数据库容器重启、应用重启及历史任务/Trace/Evidence/报告不变均已实测。
总验收仍为 `partial`，因为真实远程模式按用户选择保留待配置；不以协议替身冒充真实远程通过。
汇总见 [最终验收报告](data/benchmark/final_acceptance/acceptance.json)。
完整测试集为 183 passed，含真实 PostgreSQL/pgvector；还覆盖 Tavily 超时、选定推理分支不可达、
远程鉴权失败、配置缺失、JSON 错误、空检索、预算耗尽、证据不足、历史与回执恢复。
早期并行验证超时和模型服务重启期间的连接失败保留在
`data/cache/diagnostics/final_acceptance_attempts/`，没有写成成功验收。
恢复验收的独立 schema 会强制创建自己的表，防止复用 public 中同名表和资料。
研究 Benchmark 的旧实现已保存在 `research_v1/results/code_snapshot/`；后续界面开发改变当前代码后，
旧实验的当前源码一致性审计会按设计拒绝，应使用对应归档版本回放，不能覆盖旧 Hash。

## SFT 实验状态与已知限制

本轮没有执行 SFT，`training/` 仍是占位，主模型为原有 Qwen 推理服务。
因此没有 SFT 前后对照、训练增益、损失曲线或已训练适配器可报告；不把路由 Prompt 或受控测试当作 SFT。
后续若开展路由 SFT，应人工复核动作标签、按任务隔离训练/验证/测试集、固定模型版本，
在同一冻结 Benchmark 上记录训练配置和对照结果后再讨论提升。

当前限制包括：真实远程验收待配置、初始 Ground Truth 待人工核对、冲突套件主要覆盖表面冲突、
共享推理服务延迟不受控、扫描 PDF 无 OCR、上传二进制原件不保留。
FastAPI BackgroundTasks 不是可靠队列，恢复是数据库快照支持的手动恢复，不宣称生产级分布式恢复。
API 没有多用户鉴权与租户隔离；服务默认绑定本机，用于开发演示。
简历项目描述草稿见 [docs/resume.txt](docs/resume.txt)，只包含已经实现或实测的内容。


## Universal Research and Enterprise Foundations

New research tasks can select a validated domain profile instead of inheriting a model-infrastructure-only framing:

- `general_research`
- `technology_research`
- `market_research`
- `academic_research`
- `compliance_research`
- `business_due_diligence`
- `model_infrastructure_research`

Use `GET /api/v1/domains` to list profiles and pass `domain_id` to `POST /api/v1/research`. Existing tasks and historical benchmark data default to `general_research` through an additive schema upgrade. Domain policies now influence Claim limits, evidence freshness, independent-source corroboration, and mandatory human-review gates. Compliance and business profiles fail closed until a human approval is available; the approval workflow itself is not yet implemented.

A source connector contract is available in `app/connectors`. The initial adapters are a root-scoped read-only local File Connector, a public Web Search Connector, and a read-only PostgreSQL table connector with explicit column allowlists and optional tenant filtering; the Agent's web discovery path uses the Web Connector adapter. S3/MinIO, SharePoint, Confluence, and GitHub enterprise connectors remain future integrations. See [Domain Profiles](docs/domain_profiles.md) and [Source Connectors](docs/source_connectors.md).

OIDC JWT verification and coarse write-role checks are available in `app/auth/oidc.py`. Configure `AUTH_ENABLED=true`, `OIDC_ISSUER`, `OIDC_AUDIENCE`, and `OIDC_JWKS_URL`. Production startup requires OIDC and HTTPS identity endpoints; local development retains an explicit development-only identity. See [OIDC Authentication](docs/oidc_authentication.md).

**Important security boundary:** Database-backed tenant/project memberships, ORM read filters, write guards, and project-specific role checks are implemented. Agent tool Sessions also inherit the authenticated scope. This is application-level isolation, not PostgreSQL RLS; do not treat it as a production security certification for mutually untrusted tenants until RLS, cross-tenant penetration tests, and a complete service-path audit are finished. The human-review workflow, object storage, full observability, and multi-domain manually reviewed benchmark remain pending. See [Tenant and Project Isolation](docs/tenant_project_isolation.md) and [Enterprise Upgrade Status](docs/enterprise_upgrade_status.md).

A GitHub Actions workflow now runs Python compilation, pytest, and optional real PostgreSQL/pgvector integration tests on pushes and pull requests.


### Durable task queue (new optional production path)

Set `TASK_QUEUE_ENABLED=true` and configure `REDIS_URL` to use the transactional outbox path. Research task creation and the `outbox_events` row are committed in one PostgreSQL transaction. The dispatcher publishes pending events to Celery; the worker executes the existing LangGraph runtime. Compose now includes Redis, an outbox dispatcher, and a Celery research worker under the `apps` profile.

```bash
TASK_QUEUE_ENABLED=true docker compose --profile apps up -d --build --wait
docker compose --profile apps ps
```

Delivery is at-least-once. A crash between broker publish and outbox commit can produce a duplicate message; the worker guards execution using the existing ResearchTask/AgentRun state and task UUID. Late acknowledgements, worker-loss rejection, lease-aware retries, and expired-lease recovery reduce lost work. This is not an exactly-once guarantee. FastAPI BackgroundTasks remains the development fallback when `TASK_QUEUE_ENABLED=false`; production startup rejects that configuration.


### Database migrations

An Alembic baseline is now provided in `migrations/versions`. Development can retain the additive startup bootstrap with `DB_AUTO_MIGRATE=true`. Deployment should run:

```bash
DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DB' alembic upgrade head
```

The Compose `migration` service runs this command before the API starts. Production must set `DB_AUTO_MIGRATE=false`; the API checks for an applied Alembic version and refuses to perform schema DDL during startup. The first baseline is non-destructive and intentionally has no destructive downgrade. See [Database Migrations](docs/database_migrations.md).


### Metrics and monitoring

The API exports low-cardinality HTTP and LLM metrics at `/metrics`. With the Compose `apps` profile, Prometheus scrapes the API over the internal network and Grafana is provisioned with a Prometheus datasource:

- Prometheus: `http://127.0.0.1:9090`
- Grafana: `http://127.0.0.1:3000`

Change `GRAFANA_ADMIN_PASSWORD` before shared use. Keep `/metrics` restricted to an internal monitoring network. This is a first metrics increment; distributed OpenTelemetry traces, alert rules and log aggregation are still pending. See [Observability](docs/observability.md).


For local OIDC integration testing, an optional Keycloak realm can be started with `docker compose --profile identity up -d keycloak`. The realm includes a development CLI client and placeholder user. This is localhost-only test configuration; rotate all placeholder credentials and do not reuse it in production. See [OIDC Authentication](docs/oidc_authentication.md).


### Tenant and project isolation

The API now resolves OIDC identities to database-backed tenants and project memberships. Tenant/project filters are enforced at the SQLAlchemy session boundary, and writes outside the caller's membership are rejected. The Streamlit UI can select a project and sends `X-Project-ID`; the API exposes `/api/v1/auth/me`, `/api/v1/projects`, project membership administration, and `/api/v1/audit/events`.

Provision an OIDC identity using the exact issuer and subject claims:

```bash
python -m scripts.provision_user --tenant acme --tenant-name "Acme Research" --issuer https://id.example/realms/acme --subject '<sub>' --tenant-role RESEARCHER --project default --project-role RESEARCHER
```

This is application-level isolation, not PostgreSQL RLS. Cross-tenant penetration tests and database RLS remain required before exposing the system to mutually untrusted organizations. See [Tenant and Project Isolation](docs/tenant_project_isolation.md).


### PostgreSQL Row-Level Security

Alembic revision `20261123_0004` enables and forces tenant RLS on research tasks, documents, chunks, evidence, feedback, evaluation results, and audit events. Each PostgreSQL transaction receives a local `app.tenant_id`; missing context returns no tenant rows and rejects tenant-owned writes. Background and Celery execution messages carry tenant context before accessing protected rows.

**Deployment requirement:** run the API/worker with a dedicated least-privilege PostgreSQL role that is not a superuser and does not have `BYPASSRLS`. Superusers and BYPASSRLS roles bypass RLS even when FORCE is enabled. Project-level RLS and full penetration testing are still pending. See [Tenant and Project Isolation](docs/tenant_project_isolation.md).


For production PostgreSQL RLS deployment, create a dedicated `NOSUPERUSER NOBYPASSRLS` runtime role and set `COMPOSE_RUNTIME_DATABASE_URL`; keep `COMPOSE_DATABASE_URL` for migrations only. See [PostgreSQL RLS Deployment](docs/postgres_rls_deployment.md).


### Enterprise SSO and human review

Streamlit provides opt-in OIDC sign-in/logout using `STREAMLIT_OIDC_ENABLED=true` and the IdP **access token** via `st.user.tokens`; configure an IdP client and secure `.streamlit/secrets.toml` per [Streamlit SSO](docs/streamlit_sso.md). Reviewer decisions are immutable, independently audited and tied to a verified Evidence fingerprint; see [Human Review](docs/human_review.md). Real browser/IdP sign-in and external security assessment are not yet certified.

RLS migrations protect tenant-owned and task-linked tables; GitHub Actions tests raw SQL, write denial and pooled transactions with non-owner roles. **RLS alone is not a SQL-injection sandbox:** an arbitrary SQL executor can spoof the session tenant GUC. Global shared-cache tables are not yet tenant RLS protected. See [Tenant and Project Isolation](docs/tenant_project_isolation.md).
