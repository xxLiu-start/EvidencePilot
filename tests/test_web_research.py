from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from tavily.errors import InvalidAPIKeyError, UsageLimitExceededError

from app.config import Settings
from app.db.models import ClaimRecord, EvidenceRecord, WebContent, WebSource
from app.db.session import Base
from app.evidence.extraction import ExtractionDraft, extract_evidence, save_evidences
from app.evaluation.web_benchmark import validate_snapshot
from app.llm_client import LLMError
from app.search.cache import CacheMiss, digest
from app.search.parser import ParsedPage, PageError, assert_public_url, extract_html, fetch_page, normalize_url
from app.search.query_generator import ClaimInput, QueryPlan, generate_queries
from app.search.web_search import SearchError, WebSearchClient


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def page(url="https://example.org/guide"):
    body = "Qwen supports self-hosted deployment and function calling.\nThis documentation describes the official deployment guide."
    return ParsedPage(url, url, "Qwen guide", body, None, datetime.now(timezone.utc).isoformat(), digest(body))


def test_queries_bound_dedup_claim_binding_and_entity_drift():
    plan = QueryPlan(queries=[
        {"claim_id": "C1", "query": "Qwen official deployment"},
        {"claim_id": "C1", "query": "qwen OFFICIAL deployment!"},
        {"claim_id": "C1", "query": "Football scores today"},
        {"claim_id": "C1", "query": "DeepSeek function calling"},
    ])
    client = SimpleNamespace(complete_json=lambda *a, **k: (plan, None))
    result, _ = generate_queries([ClaimInput(claim_id="C1", claim_text="Compare Qwen and DeepSeek")], 2, client=client)
    assert len(result.queries) == 2 and result.queries[1].query == "DeepSeek function calling"
    plan.queries[0].claim_id = "UNKNOWN"
    with pytest.raises(LLMError, match="unknown_claim_id"):
        generate_queries([ClaimInput(claim_id="C1", claim_text="Qwen")], client=client)


def test_search_retry_cache_offline_and_credential_source(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path),
                   credentials_env_file=str(tmp_path / ".env"))
    (tmp_path / ".env").write_text("TAVILY_API_KEY=file-only-secret\n")
    monkeypatch.setenv("TAVILY_API_KEY", "must-not-use-environment-secret")
    calls, keys = [], []

    def search(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise UsageLimitExceededError("limited")
        return {"results": [{"url": "https://example.org/guide", "title": "guide"}]}

    def sdk(api_key):
        keys.append(api_key)
        return SimpleNamespace(search=search)

    monkeypatch.setattr("app.search.web_search.TavilyClient", sdk)
    client = WebSearchClient(cfg, sleep=lambda _: None)
    assert client.search("Qwen documentation")["attempts"] == 2
    assert keys == ["file-only-secret"] and calls[0]["timeout"] == 20
    assert client.search("Qwen documentation", cache_only=True)["cache_hit"]
    assert len(calls) == 2
    with pytest.raises(CacheMiss):
        client.search("uncached query", cache_only=True)
    sdk_fail = SimpleNamespace(search=lambda **kwargs: (_ for _ in ()).throw(InvalidAPIKeyError("private")))
    with pytest.raises(SearchError, match="authentication_or_quota"):
        WebSearchClient(cfg, sdk=sdk_fail).search("different query")


def test_url_normalization_parser_and_private_address_rejection():
    assert normalize_url("HTTPS://Example.ORG:443/guide?b=2&utm_source=x&a=1#section") == "https://example.org/guide?a=1&b=2"
    with pytest.raises(PageError):
        normalize_url("file:///etc/passwd")
    with pytest.raises(PageError):
        assert_public_url("http://127.0.0.1/metadata")
    html = '<html><head><title>Qwen guide</title></head><body><nav>MENU</nav><article><h1>Deployment</h1><p>'
    html += "Qwen provides detailed self-hosted deployment documentation and function calling examples. " * 5
    html += '</p></article><script>PRIVATE SCRIPT</script></body></html>'
    parsed = extract_html(html, "https://example.org/guide")
    assert "Qwen" in parsed.body and "PRIVATE SCRIPT" not in parsed.body


def test_url_content_dedup_cache_and_exact_evidence(db, tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path))
    calls = []
    monkeypatch.setattr("app.search.parser.download_page", lambda url, config: calls.append(url) or page(url))
    first = fetch_page(db, "https://example.org/guide?utm_source=test#part", search_query="Qwen guide", config=cfg)
    assert fetch_page(db, "https://example.org/guide", search_query="deployment", config=cfg).cache_hit
    fetch_page(db, "https://mirror.example.org/guide", config=cfg)
    assert len(calls) == 2
    assert db.scalar(select(func.count()).select_from(WebSource)) == 2
    assert db.scalar(select(func.count()).select_from(WebContent)) == 1
    assert len(list((tmp_path / "contents").glob("*.json"))) == 1
    claim = ClaimInput(claim_id="C1", claim_text="Qwen supports self-hosted deployment.")
    db.add(ClaimRecord(claim_id=claim.claim_id, claim_text=claim.claim_text))
    db.commit()
    quote = "Qwen supports self-hosted deployment and function calling."
    draft = ExtractionDraft(evidences=[{"supporting_text": quote, "relation": "supports"}])
    client = SimpleNamespace(complete_json=lambda *args, **kwargs: (draft, None))
    evidence, _ = extract_evidence(claim, first, client=client)
    save_evidences(db, evidence)
    save_evidences(db, evidence)
    assert db.scalar(select(func.count()).select_from(EvidenceRecord)) == 1
    assert evidence[0].source_url == first.source_url and evidence[0].quote_start == 0
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"claims": [claim.model_dump()], "queries": [{"claim_id": "C1", "query": "Qwen guide"}],
                                         "pages": [{k: v for k, v in asdict(first).items() if k != "body"}],
                                         "evidences": [e.model_dump(mode="json") for e in evidence]}))
    assert validate_snapshot(snapshot_path, tmp_path)["network_requests"] == 0
    content_path = tmp_path / "contents" / (first.content_hash + ".json")
    original_cache = content_path.read_text()
    content_path.write_text(json.dumps({"body": "tampered cached body"}))
    with pytest.raises(ValueError, match="hash"):
        validate_snapshot(snapshot_path, tmp_path)
    content_path.write_text(original_cache)
    forged = evidence[0].model_copy(update={"evidence_id": uuid4(), "source_url": "https://unknown.example/"})
    with pytest.raises(ValueError):
        save_evidences(db, [forged])
    draft.evidences[0].supporting_text = "Invented content never found in the original webpage."
    with pytest.raises(LLMError, match="unverifiable_quote"):
        extract_evidence(claim, first, client=client)


def test_cache_only_never_downloads_missing_page(db, tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path))
    monkeypatch.setattr("app.search.parser.download_page", lambda *a: pytest.fail("network called"))
    with pytest.raises(CacheMiss):
        fetch_page(db, "https://example.org/new", cache_only=True, config=cfg)


def test_search_5xx_retries_are_bounded(tmp_path):
    import requests

    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = 503
        raise requests.HTTPError("not logged", response=response)

    cfg = Settings(_env_file=None, database_url="sqlite://", web_cache_dir=str(tmp_path), search_max_retries=2)
    client = WebSearchClient(cfg, sdk=SimpleNamespace(search=fail), sleep=lambda _: None)
    with pytest.raises(SearchError, match="server"):
        client.search("query")
    assert len(calls) == 3
