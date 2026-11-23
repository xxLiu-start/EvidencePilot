"""Adapter exposing the existing public-web pipeline through the connector contract."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.connectors.base import ConnectorDocument, SourceConnector
from app.config import Settings, settings
from app.search.parser import fetch_page, normalize_url
from app.search.web_search import WebSearchClient


class WebSearchConnector(SourceConnector):
    """Tavily-backed public web connector.

    Search results are discovery hints only. A source becomes a fetchable document
    after the existing parser has downloaded, normalized, and snapshotted its body.
    """

    connector_id = "public_web"

    def __init__(self, db, config: Settings = settings) -> None:
        self.db = db
        self.config = config

    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None,
               access_context: dict[str, Any] | None = None) -> list[ConnectorDocument]:
        if not query.strip() or not 1 <= limit <= 10:
            raise ValueError("query must be non-empty and limit must be between 1 and 10")
        client = WebSearchClient(self.config)
        try:
            response = client.search(query, max_results=limit)
        finally:
            sdk = client._sdk
            if sdk is not None and hasattr(sdk, "session"):
                sdk.session.close()
        documents = []
        for item in response.get("results", [])[:limit]:
            url = normalize_url(item["url"])
            documents.append(ConnectorDocument(
                source_id=url, connector_id=self.connector_id, source_type="web",
                canonical_location=url, title=(item.get("title") or url)[:500],
                retrieved_at=datetime.now(timezone.utc),
                authority_level="unknown",
                metadata={"snippet": (item.get("content") or "")[:1000], "discovery_only": True},
            ))
        return documents

    def fetch(self, source_id: str, *, access_context: dict[str, Any] | None = None) -> ConnectorDocument:
        page = fetch_page(self.db, normalize_url(source_id), config=self.config)
        return ConnectorDocument(
            source_id=page.source_url, connector_id=self.connector_id, source_type="web",
            canonical_location=page.source_url, title=page.title, content=page.body,
            content_hash=page.content_hash,
            published_at=datetime.fromisoformat(page.published_at) if page.published_at else None,
            retrieved_at=datetime.fromisoformat(page.retrieved_at),
            authority_level="unknown",
            metadata={"fetched_url": page.fetched_url, "cache_hit": page.cache_hit},
        )

    def health_check(self) -> dict[str, Any]:
        return {"connector_id": self.connector_id, "status": "configured" if self.config.tavily_api_key else "credential_required"}
