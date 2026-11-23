from pathlib import Path

import pytest

from app.connectors.base import ConnectorDocument, SourceConnector
from app.connectors.files import FileConnector
from app.connectors.registry import ConnectorRegistry
from app.domains.profiles import get_domain_profile, list_domain_profiles
from app.verification.scope import EvidenceScope, scope_of


def test_builtin_domain_profiles_are_versioned_and_domain_neutral():
    profiles = {profile.domain_id: profile for profile in list_domain_profiles()}
    assert {
        "general_research",
        "technology_research",
        "market_research",
        "academic_research",
        "compliance_research",
        "business_due_diligence",
        "model_infrastructure_research",
    } <= profiles.keys()
    assert profiles["general_research"].version
    assert profiles["model_infrastructure_research"].domain_id != "general_research"


def test_unknown_domain_fails_closed():
    with pytest.raises(ValueError, match="Unknown research domain"):
        get_domain_profile("made_up_domain")


def test_scope_accepts_domain_specific_attributes_and_preserves_legacy_fields():
    scope = EvidenceScope(
        domain_id="market_research",
        attributes={"geography": "US", "period": "2025-2026", "metric": "market_share"},
        model_name="Legacy-compatible",
    )
    normalized = scope_of(scope)
    assert normalized["domain_id"] == "market_research"
    assert normalized["geography"] == "US"
    assert normalized["period"] == "2025-2026"
    assert normalized["model_name"] == "Legacy-compatible"


def test_file_connector_search_and_fetch_are_root_scoped(tmp_path: Path):
    (tmp_path / "reports").mkdir()
    document = tmp_path / "reports" / "market-overview.md"
    document.write_text("# Market Overview\n\nVerified market research text.", encoding="utf-8")
    connector = FileConnector(tmp_path)
    hits = connector.search("market", limit=5)
    assert len(hits) == 1
    fetched = connector.fetch(hits[0].source_id)
    assert fetched.content is not None
    assert "Verified market research text." in fetched.content
    assert fetched.content_hash is not None
    assert fetched.source_type == "document"


def test_file_connector_rejects_path_traversal(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    connector = FileConnector(root)
    with pytest.raises(PermissionError):
        connector.fetch("../outside.md")


def test_connector_registry_rejects_duplicate_and_unknown_connectors():
    class StubConnector(SourceConnector):
        connector_id = "stub"

        def search(self, query, *, limit=10, filters=None, access_context=None):
            return []

        def fetch(self, source_id, *, access_context=None):
            return ConnectorDocument(
                source_id=source_id, connector_id=self.connector_id, source_type="test",
                canonical_location=source_id,
            )

    registry = ConnectorRegistry()
    connector = StubConnector()
    registry.register(connector)
    assert registry.get("stub") is connector
    with pytest.raises(ValueError, match="already registered"):
        registry.register(connector)
    with pytest.raises(LookupError, match="Unknown connector"):
        registry.get("missing")



def test_source_authority_registry_handles_non_llm_domains():
    from types import SimpleNamespace

    from app.verification.verifier import source_class

    assert source_class(SimpleNamespace(source_type="web", source_url="https://www.sec.gov/Archives/example")) == "official"
    assert source_class(SimpleNamespace(source_type="web", source_url="https://eur-lex.europa.eu/legal-content/example")) == "official"
    assert source_class(SimpleNamespace(source_type="web", source_url="https://arxiv.org/abs/1234.5678")) == "academic"
    assert source_class(SimpleNamespace(source_type="web", source_url="https://unknown-example.test/article")) == "web"
    assert source_class(SimpleNamespace(source_type="document", source_url=None)) == "internal"



def test_domain_policy_requires_independent_sources_when_configured():
    from types import SimpleNamespace

    from app.verification.verifier import source_requirements

    claim = {"id": "C1", "claim": "Compare market share", "required_source": "any",
             "min_independent_sources": 2}
    one_source = [SimpleNamespace(id="1", source_type="web", source_url="https://data.example.org/a",
                                  document_id=None, scope={})]
    two_sources = [
        *one_source,
        SimpleNamespace(id="2", source_type="web", source_url="https://reports.example.net/b",
                        document_id=None, scope={}),
    ]
    assert source_requirements(claim, one_source)["source_requirements_met"] is False
    assert source_requirements(claim, two_sources)["source_requirements_met"] is True



def test_explicit_domain_scope_attributes_must_match_evidence():
    from types import SimpleNamespace

    from app.verification.verifier import eligible_source

    claim = {
        "id": "C1",
        "claim": "What is Company A's US market share in 2025?",
        "required_source": "official",
        "scope": {"domain_id": "market_research", "attributes": {"geography": "US", "period": "2025"}},
    }
    matching = SimpleNamespace(
        source_type="web", source_url="https://www.sec.gov/Archives/example",
        scope={"attributes": {"geography": "US", "period": "2025"}},
    )
    mismatched = SimpleNamespace(
        source_type="web", source_url="https://www.sec.gov/Archives/example",
        scope={"attributes": {"geography": "EU", "period": "2025"}},
    )
    assert eligible_source(claim, matching) is True
    assert eligible_source(claim, mismatched) is False



def test_postgresql_table_connector_enforces_tenant_filter_and_read_only_queries():
    from sqlalchemy import create_engine, text

    from app.connectors.postgres import PostgreSQLTableConnector

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE research_records (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, secret TEXT NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO research_records (id, tenant_id, title, body, status, secret) VALUES "
            "(1, 'tenant-a', 'Market report', 'Market growth evidence', 'approved', 'private-a'), "
            "(2, 'tenant-b', 'Market plan', 'Private tenant data', 'approved', 'private-b')"
        ))
    connector = PostgreSQLTableConnector(
        engine, table_name="research_records", primary_key="id",
        searchable_columns=["title", "body"], tenant_column="tenant_id",
        filter_columns=["status"], max_rows=10,
    )
    hits = connector.search("market", access_context={"tenant_id": "tenant-a"})
    assert len(hits) == 1
    assert hits[0].source_type == "database"
    assert hits[0].content_hash
    assert "secret" not in hits[0].metadata["record"]
    assert connector.fetch("1", access_context={"tenant_id": "tenant-a"}).title == "Market report"
    with pytest.raises(LookupError):
        connector.fetch("2", access_context={"tenant_id": "tenant-a"})
    with pytest.raises(PermissionError):
        connector.search("market")
    with pytest.raises(ValueError, match="Filter column is not allowed"):
        connector.search("market", filters={"body": "Private tenant data"},
                         access_context={"tenant_id": "tenant-a"})
    engine.dispose()
