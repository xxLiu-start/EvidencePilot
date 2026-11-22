# EvidencePilot 本地启动与 API 使用

点击各节标题展开或收起。在服务器终端中进入项目目录，按顺序启动 PostgreSQL 和 FastAPI。示例使用 API 端口 `8001`。

最终三服务演示已提供独立配置：

```bash
docker compose --env-file .env --env-file deploy/demo.env -p evidencepilot-final --profile apps up -d --build --wait
```

界面为 `http://127.0.0.1:18502`，API 文档为 `http://127.0.0.1:18002/docs`。
已有 `.env` 不要覆盖；其中保留真实模型 ID 和凭据。
`deploy/demo.env` 的容器模型地址只针对当前机器，其他机器需改为实际可达地址。
通过 SSH 使用时转发 `18502` 和需要的 API 端口。
需要停止演示时执行同一命令前缀并使用 `stop`，不会删除数据卷：

```bash
docker compose --env-file .env --env-file deploy/demo.env -p evidencepilot-final --profile apps stop
```

若采用 Conda 应用加 Docker 数据库的混合方式，可按下面各节操作，也可以使用
`python scripts/dev_stack.py start`；完整架构、实验、截图和验收边界见 README。

<details>
<summary>1. Conda 环境（创建、激活、退出、删除）</summary>

```bash
cd /data0/liuxule/project/EvidencePilot
```

仅在尚未创建 `evidencepilot` 环境时执行：

```bash
# 创建 Python 3.12 环境。
conda create -n evidencepilot python=3.12 -y
```

```bash
# 安装项目依赖，已有依赖且未发生变更时可以跳过安装。
conda activate evidencepilot
python -m pip install -r requirements.txt
```

如果创建环境时清华镜像返回 `HTTP 000`，可以试一次官方源：

```bash
# 此命令只为本次创建环境指定源。
conda create -n evidencepilot --override-channels \
  -c https://repo.anaconda.com/pkgs/main python=3.12 -y
```

项目根目录的 `.env` 应包含 `DATABASE_URL`。已有配置时直接使用。下面是格式示例，用户名、密码和数据库名应与 Compose 中的 PostgreSQL 配置一致：

```dotenv
DATABASE_URL=postgresql+psycopg://<用户名>:<密码>@localhost:5432/evidencepilot
```

### 退出与删除

```bash
# 退出当前 Conda 环境，重新使用时执行 conda activate evidencepilot。
conda deactivate
```

确认 API 已停止且不再需要这个 Python 环境后，再执行删除：

```bash
conda env remove -n evidencepilot
```

</details>

<details>
<summary>2. PostgreSQL（启动、检查、停止、删除容器）</summary>

### 启动与检查

```bash
# 检查 Compose 配置是否有效，并等待 PostgreSQL 通过健康检查。
docker compose config --quiet
docker compose up -d --wait postgres

# 查看容器状态，正常时应显示 healthy。
docker compose ps postgres

# 出现启动问题时查看最近日志。
docker compose logs --tail=100 postgres
```

### 停止容器

```bash
# 建议先停止 API，再停止其数据库。
docker compose stop postgres
```

### 删除容器

```bash
# 删除已停止的 PostgreSQL 容器，保留数据库数据卷。
docker compose rm postgres
```

以后执行 `docker compose up -d --wait postgres` 会重建容器并接回数据卷。

</details>

<details>
<summary>3. FastAPI（启动、检查、停止、删除后台会话）</summary>

### 检查是否已启动

先检查 `8001` 上是否已有 API。若健康检查返回 `{"status":"ok"}`，可直接打开文档。若原来的后台任务仍在运行，后续代码改动会自动加载。

```bash
# 直连服务器本机的 API，避免请求经过代理。
curl --noproxy 127.0.0.1 -fsS http://127.0.0.1:8001/health
```

### 启动 API

确认 `8001` 未被占用后，在项目目录激活 `evidencepilot` 环境，选择前台或后台启动。前台启动命令如下：

```bash
# 在 EvidencePilot 目录执行。按 Ctrl+C 停止前台进程。
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

需要在退出终端后继续运行时，使用 tmux：

```bash
# 使用当前 Conda 环境的 Python 启动后台会话。
tmux -L evidencepilot_api new-session -d -s api-8001 -c "$PWD" \
  "$CONDA_PREFIX/bin/python" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001

# 查看会话和最近 40 行日志。
tmux -L evidencepilot_api list-sessions
tmux -L evidencepilot_api capture-pane -p -t api-8001 -S -40
```

应用启动时会在数据库中创建缺失的 `research_tasks`、`documents`、`document_chunks` 和 `retrieval_index_revision` 表，并幂等地启用 `vector` 扩展、为已有分块表补充 BGE-M3 向量列和 HNSW 余弦索引。每个 API 进程会从数据库重建 BM25。旧分块的向量需执行补算命令；文档上传、分块、Hybrid 检索、重排和评测的用法见 [README.md](README.md)。

Day 4 同时会创建 Claim、网页来源、去重正文、搜索记录及 `evidences` 表。
启动前需配置当前 LLM 分支；当前本地 Qwen 地址为 `http://127.0.0.1:8010/v1`。
配置两种推理模式、Tavily、网页缓存和 Evidence 验收的用法也见 README。

打开 [Swagger 文档](http://127.0.0.1:8001/docs) 可在浏览器中调用 API。通过 VS Code Remote SSH 工作时，在 **Ports** 面板转发远程端口 `8001`，用面板显示的本地地址打开 `/docs`。

### 停止与删除后台会话

前台进程在启动它的终端按 `Ctrl+C`。后台进程执行：

```bash
# 向 Uvicorn 发送 Ctrl+C，让它正常退出。会话通常随进程退出而关闭。
tmux -L evidencepilot_api send-keys -t api-8001 C-c
```

如果会话仍存在，可以手动关闭。该命令也会结束会话里仍在运行的进程：

```bash
tmux -L evidencepilot_api kill-session -t api-8001
```

</details>

<details>
<summary>4. 执行 API 请求（创建、读取任务）</summary>

API 启动后，在另一个终端执行以下命令。示例会在数据库里创建一条任务记录。

```bash
API_BASE=http://127.0.0.1:8001

# 提交任务，成功时返回 HTTP 201。query 必填，budget 省略时默认为 6。
TASK_JSON=$(curl --noproxy 127.0.0.1 -fsS -X POST "$API_BASE/api/v1/research" \
  -H 'Content-Type: application/json' \
  -d '{"query":"比较 PostgreSQL 全文搜索和 pgvector 的适用场景","budget":6}')
printf '%s\n' "$TASK_JSON" | python -m json.tool

# 从创建响应中取出 UUID，再读取同一条任务。
TASK_ID=$(printf '%s\n' "$TASK_JSON" | python -c 'import json, sys; print(json.load(sys.stdin)["id"])')
curl --noproxy 127.0.0.1 -fsS "$API_BASE/api/v1/tasks/$TASK_ID" | python -m json.tool
```

新建任务的状态为 `queued`。查询不存在的任务会返回 `404`。无效的 UUID、空查询或非正数预算会返回 `422`。

在 Swagger 中，展开 `POST /api/v1/research`，点击 **Try it out**，填入示例 JSON 后点击 **Execute**。复制返回的 `id`，再到 `GET /api/v1/tasks/{task_id}` 填入并执行。

</details>

<details>
<summary>5. 执行数据库检查与初始化 SQL</summary>

```bash
# 查看 vector 扩展版本和 research_tasks 表的位置。
docker compose exec -T postgres sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT schemaname, tablename FROM pg_tables WHERE tablename = 'research_tasks';
SQL
```

如果首次初始化日志显示 `init.sql: Is a directory`，用 `ls -ld scripts/init.sql` 检查宿主机路径。当前的 Compose 要求它是已存在的普通文件。PostgreSQL 只在数据卷首次初始化时自动执行脚本。对已有数据库，可以补跑当前的 `CREATE EXTENSION IF NOT EXISTS vector`：

```bash
# 从宿主机读取脚本并传给当前数据库，保留现有数据卷。
docker compose exec -T postgres sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < scripts/init.sql
```

</details>

<details>
<summary>6. 旧 API 容器（排查、停止、删除、恢复）</summary>

如果启动 API 时提示 `Address already in use`，先查看占用端口的进程或容器：

```bash
# 查看 8000 和 8001 的监听状态。
ss -lntp '( sport = :8000 or sport = :8001 )'
docker ps --filter publish=8000

# 确认占用者是旧 API 容器后，停止它以释放 8000。
docker stop evidencepilot-api-1
```

```bash
# 确定不用这个旧容器时，可删除它以清除 Compose 的孤立容器提示。
docker rm evidencepilot-api-1
```

如果只是停止了旧容器，且确实需要恢复旧版，可在 `8000` 空闲时启动它：

```bash
docker start evidencepilot-api-1
```

</details>

<details>
<summary>7. 删除 PostgreSQL 数据卷（永久清空数据库）</summary>

仅在已备份或确认要清空整个数据库时执行。普通停止和重启用第 2 节的命令即可。

```bash
# 先停止 API，再停止并删除数据库容器。
docker compose stop postgres
docker compose rm postgres

# 确认卷名后再删除。删除后，任务记录和其他 PostgreSQL 数据都将丢失。
docker volume inspect --format '{{.Name}}' evidencepilot_postgres_data
docker volume rm evidencepilot_postgres_data
```

下一次 `docker compose up -d --wait postgres` 会初始化一个空数据库。

</details>
