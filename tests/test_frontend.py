from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from app.db.models import WebContent
from app.db.session import Base, get_db, get_cached_content
from app.main import app
from app.auth.oidc import get_request_context, get_current_principal
from frontend.api_client import APIClient, APIError
from scripts.adaptive_scenarios import run_scenario


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'frontend.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session: yield session
    engine.dispose()


def test_evidence_is_bound_to_task_not_shared_claim_id_and_detects_changed_body(db):
    get_request_context(db, get_current_principal(None), None)
    first = run_scenario(db, "sufficient_evidence")["state"]
    second = run_scenario(db, "sufficient_evidence")["state"]
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    try:
        one, two = first["task_id"], second["task_id"]
        identifier = first["evidence_ids"][0]
        listing = client.get(f"/api/v1/tasks/{one}/evidences").json()
        assert {row["evidence_id"] for row in listing["items"]} == set(first["evidence_ids"])
        assert not set(second["evidence_ids"]) & {row["evidence_id"] for row in listing["items"]}
        assert client.get(f"/api/v1/tasks/{two}/evidences/{identifier}").status_code == 404
        original = client.get(f"/api/v1/tasks/{one}/evidences/{identifier}").json()
        assert original["provenance_valid"] and original["filename"]
        assert original["supporting_text"] in original["original"]["body"]
        get_cached_content(db, original["content_hash"]).body += " changed"
        db.commit()
        changed = client.get(f"/api/v1/tasks/{one}/evidences/{identifier}").json()
        assert not changed["provenance_valid"] and changed["original"]["body"] is None
        assert client.get(f"/api/v1/tasks/{uuid4()}/evidences").status_code == 404
    finally:
        client.close(); app.dependency_overrides.clear()


def test_web_evidence_original_is_saved_snapshot_without_refetch(db, monkeypatch):
    get_request_context(db, get_current_principal(None), None)
    state = run_scenario(db, "missing_evidence")["state"]
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(httpx.Client, "get", lambda *a, **k: pytest.fail("Evidence reads must not refetch a webpage"))
    # ASGI client uses request directly, so no real network call is needed.
    client = TestClient(app)
    try:
        response = client.request("GET", f"/api/v1/tasks/{state['task_id']}/evidences/{state['evidence_ids'][0]}")
        value = response.json()
        assert response.status_code == 200 and value["source_url"].startswith("https://")
        assert value["provenance_valid"] and value["original"]["kind"] == "cached_webpage"
    finally:
        client.close(); app.dependency_overrides.clear()


def test_database_disconnect_has_sanitized_503():
    def broken():
        raise OperationalError("secret-query", {}, RuntimeError("password-secret-sentinel"))
    app.dependency_overrides[get_db] = broken
    client = TestClient(app)
    try:
        response = client.get("/api/v1/tasks")
        assert response.status_code == 503
        assert response.json()["error_category"] == "database_unavailable"
        assert "secret" not in response.text
    finally:
        client.close(); app.dependency_overrides.clear()


def test_ui_client_never_retries_ambiguous_writes_or_shows_upstream_secrets():
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-upstream-response", request=request)
    client = APIClient("http://api.test", transport=httpx.MockTransport(timeout))
    with pytest.raises(APIError, match="^timeout$"):
        client.create("test", 4)
    assert len(calls) == 1
    client.transport = httpx.MockTransport(lambda req: httpx.Response(503, text="secret upstream body"))
    with pytest.raises(APIError, match="^unavailable$"):
        client.health()
    with pytest.raises(ValueError): client.task("../../untrusted-path")
    with pytest.raises(ValueError): APIClient("http://user:password@example.com")


class UIAPI:
    def __init__(self): self.created = []
    task_id = str(uuid4())
    def health(self): return {"status": "ok"}
    def me(self): return {"projects": []}
    def system(self): return {"mode": "fixture", "model": "fixture-model"}
    def tasks(self, offset=0): return {"items": [], "total": 0}
    def create(self, query, budget):
        self.created.append((query, budget)); return {"task_id": self.task_id}
    def task(self, task): return {"id": task, "query": "UI fixture research", "status": "completed", "budget": 4}
    def trace(self, task): return {"steps": [], "policy_decisions": [], "reflections": [], "checkpoints": []}
    def report(self, task): return {"report": "Fixture report with verified evidence.", "coverage": 1, "claim_statuses": {}}
    def evidences(self, task): return {"items": [], "claims": [], "claim_statuses": {}}
    def verifications(self, task): return {"items": []}
    def documents(self, offset=0): return {"items": [], "total": 0}
    def evaluations(self, offset=0): return {"items": [], "total": 0}


@pytest.fixture
def ui(monkeypatch):
    backend = UIAPI()
    monkeypatch.setattr("frontend.api_client.APIClient", lambda _: backend)
    return backend, AppTest.from_file(Path(__file__).resolve().parents[1]/"frontend/streamlit_app.py", default_timeout=15)


def test_research_submit_poll_terminal_and_report_navigation(ui):
    backend, page = ui
    page.run()
    assert not page.exception
    page.text_area[0].set_value("根据文档核对默认参数")
    next(button for button in page.button if button.label == "开始研究").click().run()
    assert not page.exception
    assert backend.created == [("根据文档核对默认参数", 6)]
    assert page.session_state.active_task == backend.task_id
    assert not page.session_state.polling
    next(button for button in page.button if button.label == "查看研究报告").click().run()
    assert not page.exception and page.session_state.page == "Research Report"
    assert "Fixture report" in " ".join(item.value for item in page.markdown)
    assert len(backend.created) == 1


@pytest.mark.parametrize("name", ["Document Upload", "Agent Trace", "Evidence Explorer", "Research Report", "Evaluation Dashboard"])
def test_pages_render_empty_states_without_database_access(ui, name):
    _, page = ui
    page.session_state.page = name
    page.run()
    assert not page.exception


def test_api_failure_is_actionable_and_does_not_crash_page(ui, monkeypatch):
    backend, page = ui
    def down(*a, **k): raise APIError("unreachable")
    monkeypatch.setattr(backend, "tasks", down)
    page.run()
    assert not page.exception
    assert any("无法连接" in item.value for item in page.error)



def test_api_client_forwards_per_session_bearer_token():
    seen = []
    transport = httpx.MockTransport(
        lambda request: seen.append((request.headers.get("Authorization"), request.headers.get("X-Project-ID")))
        or httpx.Response(200, json={"status": "ok"})
    )
    client = APIClient("http://api.test", transport=transport, access_token="session-access-token",
                       project_id="00000000-0000-0000-0000-000000000001")
    assert client.health() == {"status": "ok"}
    assert seen == [("Bearer session-access-token", "00000000-0000-0000-0000-000000000001")]
