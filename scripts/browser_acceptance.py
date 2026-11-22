"""Real browser → Streamlit → FastAPI → PostgreSQL → Qwen/BGE acceptance and demo.

Install Playwright separately; no model/API response is mocked in this script.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:18002")
    parser.add_argument("--ui", default="http://127.0.0.1:18502")
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/final_acceptance/browser"))
    parser.add_argument("--screenshots", type=Path, default=Path("docs/demo"))
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--seconds", type=int, default=220)
    args = parser.parse_args()
    if args.record and not 180 <= args.seconds <= 280: parser.error("Demo duration must be 180–280 seconds")
    args.output.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "kind": "live_browser_end_to_end", "checks": []}
    def passed(name, **details):
        report["checks"].append({"name": name, "passed": True, **details})
        print("PASS "+name, flush=True)
    with httpx.Client(base_url=args.api, timeout=10, trust_env=False) as api, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        options = {"viewport": {"width": 1440, "height": 1000}, "device_scale_factor": 1, "locale": "zh-CN"}
        if args.record: options.update(record_video_dir=str(args.screenshots/"raw"), record_video_size={"width":1440,"height":1000})
        context = browser.new_context(**options)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(type(exc).__name__))
        start = time.monotonic()
        def ready():
            page.wait_for_function("document.querySelector('.stApp')?.getAttribute('data-test-script-state') === 'notRunning'", timeout=60000)
            page.wait_for_timeout(250)
            expect(page.locator('[data-testid="stException"]')).to_have_count(0)
        def nav(name):
            page.locator('[data-testid="stSidebar"]').get_by_text(re.compile(name)).click()
            ready()
            expect(page.get_by_role("radio", name=re.compile(name))).to_be_checked()
            page.evaluate("window.scrollTo(0,0)")
        def shot(name):
            ready(); page.screenshot(path=str(args.screenshots/f"{name}.png"), full_page=True)
        def caption(text):
            if args.record:
                page.evaluate("""text => { let el=document.getElementById('demo-caption');
                    if(!el){el=document.createElement('div');el.id='demo-caption';document.body.appendChild(el)}
                    el.textContent=text;el.style.cssText='position:fixed;bottom:18px;left:330px;right:28px;z-index:999999;background:#15243bef;color:#fff;border-radius:10px;padding:15px 22px;font:18px sans-serif;pointer-events:none'; }""", text)
        def pace(fraction):
            if args.record:
                deadline=start+args.seconds*fraction
                while time.monotonic() < deadline: page.wait_for_timeout(min(1000,int((deadline-time.monotonic())*1000)+1))
        try:
            page.goto(args.ui, wait_until="domcontentloaded")
            ready()
            expect(page.get_by_role("radio")).to_have_count(6)
            shot("workspace")
            caption("1 / 6 · 上传资料：PDF、DOCX、Markdown 统一解析、切分并向量化")
            nav("Document Upload")
            filename=f"demo-parameters-{uuid4().hex[:8]}.md"
            content="# EvidencePilot 默认参数\n\nEvidencePilot 的默认 Chunk Size 为 500 tokens，Overlap 为 80 tokens。\n\n这份演示资料用于核对内部配置，不涉及敏感信息。\n"
            page.locator('input[type="file"]').set_input_files({"name": filename, "mimeType": "text/markdown", "buffer": content.encode()})
            page.get_by_role("button",name="上传并建立索引",exact=True).click()
            expect(page.get_by_text(re.compile("已保存 .*共 .* 个 Chunk"))).to_be_visible(timeout=300000)
            shot("documents")
            docs=api.get("/api/v1/documents").json()["items"]
            document=next(doc for doc in docs if doc["filename"]==filename)
            chunks=api.get(f"/api/v1/documents/{document['id']}/chunks").json()
            assert all(chunk["embedding_dimensions"]==1024 for chunk in chunks)
            passed("ui_document_upload_real_bge_postgresql", document_id=document["id"], dimensions=1024)
            pace(.15)
            nav("Research Workspace")
            caption("2 / 6 · 创建研究：服务返回 task_id，页面自动轮询并展示工具步骤")
            query="根据内部文档，仅核对一个 Claim：EvidencePilot 默认 Chunk Size 和 Overlap 各是多少？"
            page.get_by_label("研究任务", exact=True).fill(query)
            page.get_by_role("button",name="开始研究",exact=True).click()
            page.wait_for_url(re.compile(r".*[?&]task="), timeout=30000)
            identifier=parse_qs(urlsplit(page.url).query)["task"][0]
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                task=api.get(f"/api/v1/tasks/{identifier}").json()
                if task["status"] in {"completed","partial","failed"}:break
                page.wait_for_timeout(1000)
            else:raise TimeoutError("Research did not finish within the browser acceptance deadline")
            result=api.get(f"/api/v1/tasks/{identifier}/report").json()
            report.update(task_id=identifier,query=query,document_id=document["id"],task_status=task["status"])
            assert task["status"]=="completed" and result["coverage"]==1,task["status"]
            assert "500" in result["report"] and "80" in result["report"]
            report.update(task_id=identifier,query=query,document_id=document["id"])
            passed("ui_research_creation_polling_completed_report", task_id=identifier, coverage=result["coverage"])
            page.wait_for_timeout(2500);shot("research-completed")
            pace(.40)
            nav("Agent Trace")
            caption("3 / 6 · Action–Observation：每步保留参数、结果、耗时及实际模型 ID")
            trace=api.get(f"/api/v1/tasks/{identifier}/trace").json()
            assert len(trace["steps"])>=2 and trace["checkpoints"]
            step=trace["steps"][0]
            expect(page.get_by_text(re.compile("01 · rag_retrieve_tool"))).to_be_visible()
            page.get_by_text(re.compile("01 · rag_retrieve_tool")).click()
            shot("trace")
            system=api.get("/api/v1/system").json()
            assert all(s["trace"]["mode"]==system["mode"] and s["trace"]["actual_model"]==system["model"] for s in trace["steps"] if s.get("trace"))
            report.update(inference=system,trace=trace)
            passed("trace_actions_observations_mode_and_actual_model", mode=system["mode"], model=system["model"], steps=len(trace["steps"]))
            pace(.55)
            nav("Evidence Explorer")
            caption("4 / 6 · 点击 Evidence ID，核对原文、来源位置与 Hash；仅检索到文字不算验证通过")
            evidence=api.get(f"/api/v1/tasks/{identifier}/evidences").json()
            assert evidence["items"]
            row=evidence["items"][0]
            page.get_by_role("button", name=f"Evidence {row['evidence_id']}", exact=True).click()
            expect(page.get_by_role("dialog")).to_be_visible()
            original=api.get(f"/api/v1/tasks/{identifier}/evidences/{row['evidence_id']}").json()
            assert original["provenance_valid"] and original["supporting_text"] in original["original"]["body"]
            assert hashlib.sha256(original["original"]["body"].encode()).hexdigest()==original["content_hash"]
            shot("evidence")
            passed("click_evidence_id_displays_original_with_provenance", evidence_id=row["evidence_id"])
            pace(.70)
            page.keyboard.press("Escape")
            nav("Research Report")
            caption("5 / 6 · 最终报告：已验证结论、来源引用、证据缺口和下载入口")
            expect(page.get_by_role("button",name="下载研究报告",exact=True)).to_be_visible()
            with page.expect_download() as download:
                page.get_by_role("button",name="下载研究报告",exact=True).click()
            download.value.save_as(str(args.output/"downloaded-report.md"))
            assert "500" in (args.output/"downloaded-report.md").read_text()
            shot("report")
            passed("report_download_and_evidence_index")
            pace(.85)
            nav("Evaluation Dashboard")
            caption("6 / 6 · 冻结实验：三种 Baseline、三组消融；初始标注待人工复核，不预设效果提升")
            expect(page.get_by_role("button",name="下载指标 CSV",exact=True)).to_be_visible()
            expect(page.get_by_text("初始标注待人工复核。这些规则分数不能证明普遍优于 Baseline。")).to_be_visible()
            shot("evaluations")
            passed("evaluation_dashboard_via_api")
            page.reload();ready()
            nav("Research Workspace")
            expect(page.get_by_text(query,exact=True).first).to_be_visible()
            assert parse_qs(urlsplit(page.url).query)["task"]==[identifier]
            shot("history")
            passed("history_after_browser_reload")
            assert not errors,errors
            caption("研究完成 · 文档、任务、证据、步骤与报告均已持久化；可从历史任务继续查看")
            pace(1)
            report.update(status="passed", finished_at=datetime.now(timezone.utc).isoformat(), browser_errors=errors)
        except Exception as exc:
            report.update(status="failed",error_category=type(exc).__name__)
            page.screenshot(path=str(args.screenshots/"failure.png"),full_page=True)
            raise
        finally:
            video=page.video if args.record else None
            context.close()
            if video:
                video.save_as(str(args.screenshots/"evidencepilot-demo.webm"))
                report["video_wall_duration_seconds"]=time.monotonic()-start
            browser.close()
            (args.output/"acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")


if __name__ == "__main__": main()
