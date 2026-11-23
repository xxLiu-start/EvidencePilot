"""Six-page Streamlit client. All application data comes from FastAPI."""
import json
import os
from pathlib import Path
import sys
import time
from uuid import UUID

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frontend.api_client import APIClient, APIError, safe_http_url
from frontend.auth import access_token_from_identity

st.set_page_config(page_title="EvidencePilot · 证据驱动研究", page_icon="🔎", layout="wide")
PAGES = {"Research Workspace": "研究工作区", "Document Upload": "文档上传",
         "Agent Trace": "执行轨迹", "Evidence Explorer": "证据浏览器",
         "Research Report": "研究报告", "Evaluation Dashboard": "评测看板"}
TERMINAL = {"completed", "partial", "failed"}
STATUS = {"queued": "排队中", "running": "研究中", "completed": "已完成", "partial": "证据不足 / 部分完成", "failed": "执行失败"}
ERRORS = {"timeout": "请求超时。若刚提交了任务或文档，请先查询历史记录，避免重复提交。",
          "unreachable": "无法连接 API，请检查服务是否启动。", "not_found": "记录不存在或不属于当前任务。",
          "conflict": "当前状态不允许此操作；活动任务须等待租约结束，已完成任务无需恢复。",
          "validation": "输入未通过校验，请检查内容和参数。", "file_type": "请选择 PDF、DOCX 或 Markdown。",
          "unavailable": "后端依赖暂不可用，请检查数据库或模型服务。", "too_large": "文件超过服务限制。",
          "invalid_response": "API 返回了无效响应。", "http_error": "API 请求失败，请检查服务日志中的错误类别。"}


def api():
    # Keep the legacy one-argument constructor contract so existing APIClient
    # test doubles and injected clients remain compatible.
    client = APIClient(os.environ.get("API_BASE_URL", "http://127.0.0.1:8001"))
    if os.environ.get("STREAMLIT_OIDC_ENABLED", "false").lower() == "true":
        token = access_token_from_identity(st.user)
    else:
        token = st.session_state.get("oidc_access_token")
    if token and hasattr(client, "access_token"):
        client.access_token = token
    project_id = st.session_state.get("project_id")
    if project_id and hasattr(client, "project_id"):
        client.project_id = project_id
    return client


def error(exc):
    st.error(ERRORS.get(exc.category, "请求失败，请稍后刷新。"))


def select_task(value):
    value = str(UUID(str(value)))
    st.session_state.active_task = value
    st.session_state.polling = True
    st.session_state.poll_started = time.monotonic()
    st.query_params["task"] = value


def select_page(page):
    st.session_state.page = page


def active_task():
    value = st.session_state.get("active_task")
    if not value:
        st.info("请先在研究工作区创建或打开一个任务。")
    return value


@st.dialog("原始证据", width="large")
def evidence_dialog(evidence_id):
    try:
        item = api().evidence(st.session_state.active_task, evidence_id)
        st.caption(f"{item['claim_id']} · {item['evidence_id']}")
        st.write(item["claim_text"])
        if item["provenance_valid"]:
            st.success("原文片段、位置与已保存来源 Hash 一致。此检查不等于 Claim 语义验证。")
        else:
            st.warning("来源完整性校验未通过，需检查来源是否更新或缺失。")
        st.text(item["supporting_text"])
        url = item.get("source_url")
        if safe_http_url(url):
            st.link_button("打开来源网页", url)
        st.write({key: item.get(key) for key in ["source_type", "filename", "page", "section", "version", "published_at", "retrieved_at", "scope", "content_hash"]})
        original = item["original"]
        st.caption(original["note"])
        if original["body"] is not None:
            with st.expander("查看保存的完整正文 / 文档 Chunk"):
                st.text_area("来源正文", original["body"], height=300, disabled=True)
        else:
            st.warning("来源正文缺失或 Hash 不符，不能展示为可信原文。")
    except APIError as exc:
        error(exc)


def evidence_buttons(items, prefix):
    for item in items:
        identifier = item if isinstance(item, str) else item["evidence_id"]
        if st.button(f"Evidence {identifier}", key=f"{prefix}-{identifier}", help="查看原文、来源与完整性校验"):
            evidence_dialog(identifier)


def step_cards(trace, *, compact=False):
    steps = trace.get("steps", [])
    if not steps:
        st.info("正在规划 Claims，尚无工具执行记录。")
    for step in (steps[-3:] if compact else steps):
        label = f"{step['step_index']:02d} · {step['tool_name']} · {step['status']} · {step['elapsed_ms']/1000:.1f}s"
        with st.expander(label, expanded=compact):
            observation = step.get("observation") or {}
            st.write(observation.get("summary", "工具正在执行，尚未提交 Observation。"))
            if step.get("error_category"):
                st.warning(f"错误类别：{step['error_category']}")
            if not compact:
                st.caption("Action 参数")
                st.json(step["arguments"], expanded=False)
                st.caption("Observation")
                st.json(observation, expanded=False)
                if step.get("trace"):
                    st.caption("推理模式、模型与耗时")
                    st.json(step["trace"], expanded=False)
            evidence_buttons(observation.get("evidence_ids", []), f"step-{step['id']}")


def research_report(task_id, report):
    if not report.get("report"):
        st.info("报告尚未生成。任务结束后会自动读取；失败任务可在执行轨迹中诊断。")
        return
    st.markdown(report["report"], unsafe_allow_html=False)
    st.download_button("下载研究报告", report["report"], file_name=f"research-{task_id}.md", mime="text/markdown")
    items = api().evidences(task_id)["items"]
    with st.expander("报告证据索引（点击 Evidence ID 查看原文）", expanded=True):
        evidence_buttons(items, "report")
    with st.form("feedback"):
        rating = st.slider("报告评分", 1, 5, 4)
        comment = st.text_input("反馈（可选）", max_chars=2000)
        if st.form_submit_button("保存反馈"):
            api().feedback(task_id, rating, comment)
            st.success("反馈已保存。")


@st.fragment(run_every=2 if st.session_state.get("polling", False) else None)
def current_task(view="workspace"):
    task_id = st.session_state.get("active_task")
    if not task_id:
        return
    try:
        task = api().task(task_id)
        trace = api().trace(task_id)
        report = api().report(task_id)
        if st.session_state.get("polling") and (task["status"] in TERMINAL or
                time.monotonic()-st.session_state.get("poll_started", 0) > 900):
            st.session_state.polling = False
            st.rerun()
        st.caption(f"任务 ID · {task_id}")
        st.write(task["query"])
        cols = st.columns(4)
        cols[0].metric("任务状态", STATUS.get(task["status"], task["status"]))
        cols[1].metric("已验证覆盖率", f"{report.get('coverage',0):.0%}")
        cols[2].metric("工具步骤", f"{len(trace['steps'])} / {task['budget']}")
        cols[3].metric("已规划 Claims", len(report.get("claim_statuses", {})))
        st.progress(min(max(report.get("coverage", 0), 0), 1))
        if task["status"] == "failed":
            st.error("任务执行失败。请查看下方步骤、错误类别或服务日志；不会切换到另一推理后端。")
        elif task["status"] == "partial":
            st.warning("证据不足的部分已保留缺口并弃答，请结合来源查看报告。")
        if task["status"] not in TERMINAL:
            st.caption("每 2 秒刷新；跟踪 15 分钟后暂停，可点击继续跟踪。关闭页面不会取消后端任务。")
            if st.button("继续跟踪", key="track"):
                select_task(task_id)
                st.rerun()
        if st.button("刷新任务", key="refresh-task"):
            st.rerun()
        if view == "report":
            research_report(task_id, report)
        else:
            step_cards(trace, compact=view == "workspace")
            if view == "trace":
                for key, label in [("policy_decisions", "策略决策"), ("reflections", "失败反思与 Query 改写"), ("checkpoints", "恢复快照")]:
                    with st.expander(label): st.json(trace.get(key, []), expanded=False)
                if st.button("从最近快照恢复未完成任务", help="只恢复未完成步骤；活动租约或已结束任务会被后端拒绝。"):
                    api().recover(task_id)
                    select_task(task_id)
                    st.rerun()
            if report.get("report") and st.button("查看研究报告", on_click=select_page, args=("Research Report",)):
                st.rerun()
    except APIError as exc:
        if st.session_state.get("polling"):
            st.session_state.polling = False
            st.rerun()
        error(exc)


def workspace():
    st.title("让结论有据可查")
    st.caption("拆解问题 · 按需检索 · 验证证据 · 保留不确定性")
    with st.form("research"):
        query = st.text_area("研究任务", placeholder="例如：根据内部文档，核对 EvidencePilot 默认 Chunk Size 和 Overlap。", height=110, max_chars=4000)
        budget = st.slider("工具调用预算", 1, 12, 6, help="包含检索、打开来源和验证；检索动作另有最多 6 次的限制。")
        submit = st.form_submit_button("开始研究", type="primary", use_container_width=True)
        if submit:
            if not query.strip():
                st.warning("请输入研究任务。")
            else:
                created = api().create(query.strip(), budget)
                select_task(created["task_id"])
                st.rerun()
    if st.session_state.get("active_task"):
        st.subheader("当前研究")
        current_task()
    st.subheader("历史任务")
    offset = st.session_state.setdefault("history_offset", 0)
    history = api().tasks(offset)
    if not history["items"]:
        st.info("还没有研究任务。上传资料后，可以开始第一项研究。")
    for task in history["items"]:
        with st.container(border=True):
            left, right = st.columns([5, 1])
            left.write(task["query"])
            left.caption(f"{STATUS.get(task['status'], task['status'])} · {task['created_at']} · {task['id']}")
            if right.button("打开", key=f"history-{task['id']}"):
                select_task(task["id"])
                st.rerun()
    prev, nxt, count = st.columns([1, 1, 4])
    if prev.button("上一页", disabled=offset == 0, key="history-prev"):
        st.session_state.history_offset = max(0, offset-20); st.rerun()
    if nxt.button("下一页", disabled=offset+20 >= history["total"], key="history-next"):
        st.session_state.history_offset = offset+20; st.rerun()
    count.caption(f"共 {history['total']} 个任务")


def documents():
    st.title("Document Upload")
    st.caption("上传后自动解析、切分并保存 1024 维向量，可供 Agent 按需检索。")
    with st.form("upload"):
        file = st.file_uploader("选择研究资料", type=["pdf", "docx", "md", "txt"])
        left, right = st.columns(2)
        source = left.text_input("来源（可选）", help="文件出处或官方 URL；上传文件名不会自动成为官方来源。")
        version = right.text_input("文档版本", "1", max_chars=200)
        left, right = st.columns(2)
        size = left.number_input("Chunk Size（tokens）", min_value=2, max_value=8192, value=500)
        overlap = right.number_input("Overlap（tokens）", min_value=0, max_value=8191, value=80)
        if st.form_submit_button("上传并建立索引", type="primary"):
            if not file or not version.strip() or overlap >= size:
                st.warning("请选择文档并填写版本，Overlap 须小于 Chunk Size。")
            else:
                fields = {"version": version.strip(), "chunk_size": str(size), "chunk_overlap": str(overlap)}
                if source.strip(): fields["source"] = source.strip()
                with st.spinner("正在解析并建立索引，首次加载模型可能较慢…"):
                    result = api().upload(file.name, file.getvalue(), **fields)
                st.success(f"已保存 {result['filename']}，共 {result['chunk_count']} 个 Chunk。")
    offset = st.session_state.setdefault("document_offset", 0)
    result = api().documents(offset)
    st.subheader(f"文档库 · {result['total']}")
    if result["items"]:
        st.dataframe(pd.DataFrame(result["items"]), hide_index=True, use_container_width=True)
        chosen = st.selectbox("查看文档 Chunk", result["items"], format_func=lambda x: f"{x['filename']} · {x['id']}")
        if st.button("读取 Chunk"):
            for chunk in api().chunks(chosen["id"]):
                with st.expander(f"Chunk {chunk['chunk_index']} · {chunk['token_count']} tokens · page {chunk['page']}"):
                    st.write({k: chunk[k] for k in ["id", "document_id", "section", "source", "version", "embedding_dimensions"]})
                    st.text(chunk["text"])
    else:
        st.info("文档库为空。支持文字型 PDF、DOCX、Markdown；扫描版 PDF 暂不做 OCR。")
    prev, nxt = st.columns(2)
    if prev.button("上一页", disabled=offset == 0, key="document-prev"):
        st.session_state.document_offset = max(0, offset-20); st.rerun()
    if nxt.button("下一页", disabled=offset+20 >= result["total"], key="document-next"):
        st.session_state.document_offset = offset+20; st.rerun()


def explorer():
    st.title("Evidence Explorer")
    if not (task_id := active_task()): return
    result = api().evidences(task_id)
    st.caption(f"当前任务 · {task_id}")
    if not result["claims"]: st.info("尚未生成 Claim。")
    for claim in result["claims"]:
        status = result["claim_statuses"].get(claim["id"], {})
        with st.container(border=True):
            st.subheader(f"{claim['id']} · {claim['claim']}")
            st.caption(f"{status.get('status', 'NOT_RETRIEVED')} · 来源要求 {claim.get('required_source', 'any')} · 优先级 {claim.get('priority','—')}")
            st.write(status.get("summary", "尚无验证结果。"))
            items = [row for row in result["items"] if row["claim_id"] == claim["id"]]
            if not items: st.info("此 Claim 尚无证据。")
            for item in items:
                st.text(item["supporting_text"][:500])
                st.caption(f"{item['source_type']} · {item.get('source_url') or item.get('filename')} · {item['relation']}")
            evidence_buttons(items, f"explorer-{claim['id']}")
    with st.expander("Claim 验证记录"):
        st.json(api().verifications(task_id)["items"], expanded=False)


def evaluations():
    st.title("Evaluation Dashboard")
    st.caption("展示已保存的检索与研究实验；刷新页面不会重新运行 Benchmark。")
    rows, offset = [], 0
    while True:
        batch = api().evaluations(offset)
        rows.extend(batch["items"])
        offset += len(batch["items"])
        if not batch["items"] or offset >= batch["total"] or offset >= 1000: break
    if not rows:
        st.info("暂无已导入评测结果。API 启动时会导入本地冻结实验报告。")
        return
    rows.sort(key=lambda x: ("research_v1/results/results.json" not in x["name"], x["name"]))
    choice = st.selectbox("选择实验", rows, format_func=lambda x: x["name"])
    result = choice["result"]
    meta = result.get("metadata", {})
    if meta.get("human_reviewed") is False or result.get("ground_truth_status") == "pending_human_review":
        st.warning("初始标注待人工复核。这些规则分数不能证明普遍优于 Baseline。")
    if meta:
        model = meta.get("model", {})
        st.caption(f"模式 {model.get('mode', '—')} · 模型 {model.get('requested_model', '—')} · 状态 {meta.get('status', '—')}")
    summary = result.get("summary", [])
    if isinstance(summary, dict):
        summary = [{"method": key, **value} for key, value in summary.items()]
    if summary:
        frame = pd.DataFrame(summary).drop(columns=["counts"], errors="ignore")
        st.dataframe(frame, hide_index=True, use_container_width=True)
        if "method" in frame and "answer_accuracy" in frame:
            st.bar_chart(frame.set_index("method")[["answer_accuracy", "evidence_coverage"]], horizontal=True)
            st.caption("空值表示指标未定义或用量缺失；延迟来自共享推理服务。网页资料与 Prompt 固定。")
        st.download_button("下载指标 CSV", frame.to_csv(index=False).encode("utf-8-sig"), "evaluation.csv", mime="text/csv")
    with st.expander("实验配置与完整记录"):
        st.json(result, expanded=False)


def main():
    if os.environ.get("STREAMLIT_OIDC_ENABLED", "false").lower() == "true":
        if not st.user.is_logged_in:
            st.title("EvidencePilot · 企业登录")
            st.caption("使用组织的 OIDC 身份提供商完成安全登录。")
            if st.button("使用企业账号登录", type="primary"):
                st.login()
            st.stop()
        if access_token_from_identity(st.user) is None:
            st.error("身份提供商未返回 API access token。请配置 Streamlit expose_tokens=['access'] 和正确的 API audience。")
            st.stop()
        with st.sidebar:
            if st.button("退出企业登录"):
                for key in ("oidc_access_token", "project_id", "active_task"):
                    st.session_state.pop(key, None)
                st.logout()
    st.markdown("""<style>
    .block-container {max-width:1200px;padding-top:2rem;padding-bottom:3rem}
    [data-testid="stSidebar"] {border-right:1px solid #dce4ee}
    [data-testid="stMetric"] {background:white;border:1px solid #dce4ee;border-radius:12px;padding:16px}
    h1,h2,h3 {letter-spacing:-.025em}
    </style>""", unsafe_allow_html=True)
    if "active_task" not in st.session_state and st.query_params.get("task"):
        try: select_task(st.query_params["task"])
        except ValueError: st.warning("链接中的任务 ID 无效，请重新选择任务。")
    with st.sidebar:
        st.title("🔎 EvidencePilot")
        st.caption("研究有路径，结论有证据。")
        st.divider()
        page = st.radio("工作区导航", list(PAGES), key="page", format_func=lambda x: f"{PAGES[x]} · {x}")
        st.divider()
        with st.form("open-task"):
            task_id = st.text_input("打开历史任务 ID", placeholder="粘贴 task_id")
            if st.form_submit_button("打开任务"):
                try:
                    api().task(task_id)
                    select_task(task_id)
                    st.rerun()
                except ValueError: st.error("请输入有效的任务 UUID。")
                except APIError as exc: error(exc)
        try:
            api().health()
            membership = api().me()
            projects = membership.get("projects", [])
            project_ids = [str(item["id"]) for item in projects]
            if project_ids:
                current_project = st.session_state.get("project_id")
                if current_project not in project_ids:
                    current_project = project_ids[0]
                if len(projects) > 1:
                    selected_project = st.selectbox(
                        "当前项目", options=project_ids,
                        index=project_ids.index(current_project),
                        format_func=lambda value: next((item["name"] for item in projects if str(item["id"]) == value), value),
                    )
                    if selected_project != st.session_state.get("project_id"):
                        st.session_state.project_id = selected_project
                        st.rerun()
                else:
                    st.session_state.project_id = project_ids[0]
            config = api().system()
            st.caption(f"● API 已连接 · {config['mode']}")
            st.caption(config["model"])
        except APIError:
            st.caption("○ API 暂不可用")
    try:
        if page == "Research Workspace": workspace()
        elif page == "Document Upload": documents()
        elif page == "Agent Trace":
            st.title("Agent Trace")
            if active_task(): current_task("trace")
        elif page == "Evidence Explorer": explorer()
        elif page == "Research Report":
            st.title("Research Report")
            if active_task(): current_task("report")
        elif page == "Evaluation Dashboard": evaluations()
    except APIError as exc:
        error(exc)
    except ValueError:
        st.error("配置或记录格式无效，请检查 API_BASE_URL 与任务 ID。")


if __name__ == "__main__":
    main()
