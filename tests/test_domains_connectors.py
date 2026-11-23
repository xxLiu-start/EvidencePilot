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
