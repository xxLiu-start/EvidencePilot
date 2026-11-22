"""Live outage/restart checks, limited to the dedicated final-demo Compose project."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="evidencepilot-final")
    parser.add_argument("--api-port", type=int, default=18002)
    parser.add_argument("--ui-port", type=int, default=18502)
    parser.add_argument("--db-port", type=int, default=15432)
    parser.add_argument("--task-file", type=Path, default=Path("data/benchmark/final_acceptance/video/acceptance.json"))
    parser.add_argument("--apps", choices=["hybrid", "compose"], default="compose")
    parser.add_argument("--compose-env", type=Path, default=Path("deploy/demo.env"), help="Non-secret Compose override env file")
    args = parser.parse_args()
    if not args.project.startswith("evidencepilot-final"):
        parser.error("Only the dedicated evidencepilot-final project may be interrupted")
    output=ROOT/"data/benchmark/final_acceptance"/f"deployment-{args.apps}"
    output.mkdir(parents=True, exist_ok=True)
    data=json.loads(args.task_file.read_text())
    assert data["status"] == "passed"
    task_id=data["task_id"]
    env={**os.environ,"POSTGRES_PORT":str(args.db_port),"API_PORT":str(args.api_port),"STREAMLIT_PORT":str(args.ui_port),"EMBEDDING_LOCAL_FILES_ONLY":"true"}
    compose=["docker","compose","--env-file",".env"]
    if args.compose_env: compose.extend(["--env-file",str(args.compose_env)])
    compose.extend(["-p",args.project,"--profile","apps"])
    report={"started_at":datetime.now(timezone.utc).isoformat(),"kind":"live_owned_service_outage_and_restart","task_id":task_id,"checks":[]}
    def command(parts):
        subprocess.run(parts,cwd=ROOT,env=env,check=True,timeout=90,stdout=subprocess.DEVNULL)
    def passed(name):
        report["checks"].append({"name":name,"passed":True});print("PASS "+name,flush=True)
    api=httpx.Client(base_url=f"http://127.0.0.1:{args.api_port}",timeout=10,trust_env=False)
    def wait():
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            try:
                if api.get("/health").status_code==200 and httpx.get(
                        f"http://127.0.0.1:{args.ui_port}/_stcore/health",timeout=3,trust_env=False).status_code==200:return
            except httpx.HTTPError:pass
            time.sleep(.3)
        raise TimeoutError("API did not become healthy after service recovery")
    def get(path):
        response=api.get(path);response.raise_for_status();return response.json()
    def browser_check(expect_failure,filename):
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
            page=browser.new_page(viewport={"width":1440,"height":1000})
            page.goto(f"http://127.0.0.1:{args.ui_port}/?task={task_id}")
            if expect_failure:
                expect(page.get_by_text("后端依赖暂不可用，请检查数据库或模型服务。").first).to_be_visible(timeout=30000)
            else:
                expect(page.get_by_text(data["query"],exact=True).first).to_be_visible(timeout=30000)
                expect(page.get_by_text("已完成",exact=True).first).to_be_visible(timeout=30000)
            page.wait_for_function("document.querySelector('.stApp')?.getAttribute('data-test-script-state') === 'notRunning'",timeout=30000)
            expect(page.locator('[data-testid="stException"]')).to_have_count(0)
            page.screenshot(path=str(output/filename),full_page=True)
            browser.close()
    try:
        tasks=get("/api/v1/tasks?limit=100")["items"]
        assert all(item["status"] in {"completed","partial","failed"} for item in tasks),"Wait for active demo tasks before outage injection"
        before={"report":get(f"/api/v1/tasks/{task_id}/report"),"trace":get(f"/api/v1/tasks/{task_id}/trace"),"evidences":get(f"/api/v1/tasks/{task_id}/evidences")}
        command(compose+["stop","postgres"])
        try:
            failed=api.get(f"/api/v1/tasks/{task_id}")
            assert failed.status_code==503 and failed.json()["error_category"]=="database_unavailable"
            assert api.get("/health").status_code==503
            browser_check(True,"database-unavailable.png")
            passed("real_postgresql_disconnect_api_503_and_ui_diagnostic")
        finally:
            command(compose+["up","-d","--wait","postgres"])
            wait()
        assert get(f"/api/v1/tasks/{task_id}/report")==before["report"]
        assert get(f"/api/v1/tasks/{task_id}/evidences")==before["evidences"]
        passed("database_container_restart_persists_report_and_evidence")
        if args.apps=="hybrid":
            stack=[sys.executable,"scripts/dev_stack.py"]
            ports=["--api-port",str(args.api_port),"--ui-port",str(args.ui_port)]
            command(stack+["stop",*ports]);command(stack+["start",*ports])
        else:
            command(compose+["restart","api","streamlit"])
        wait()
        assert get(f"/api/v1/tasks/{task_id}/report")==before["report"]
        assert get(f"/api/v1/tasks/{task_id}/trace")==before["trace"]
        assert get(f"/api/v1/tasks/{task_id}/evidences")==before["evidences"]
        browser_check(False,"history-after-restart.png")
        passed("application_restart_history_trace_report_and_evidence_unchanged")
        report["status"]="passed"
    finally:
        api.close()
        report.setdefault("status","failed")
        report["finished_at"]=datetime.now(timezone.utc).isoformat()
        (output/"acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")


if __name__=="__main__":main()
