"""Domain-neutral source connector contracts.

Connectors must enforce the caller's source-system permissions. The research engine
never treats connector visibility as permission to disclose content to another user.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectorDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=2048)
    connector_id: str = Field(min_length=1, max_length=64)
    source_type: str = Field(min_length=1, max_length=64)
    canonical_location: str = Field(min_length=1, max_length=4096)
    title: str = Field(default="", max_length=500)
    content: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    published_at: datetime | None = None
    modified_at: datetime | None = None
    retrieved_at: datetime | None = None
    version: str | None = Field(default=None, max_length=200)
    authority_level: str = Field(default="unknown", max_length=64)
    access_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceConnector(ABC):
    """Minimal connector contract shared by web, files, databases and SaaS sources."""

    connector_id: str

    @abstractmethod
    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None,
               access_context: dict[str, Any] | None = None) -> list[ConnectorDocument]:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, source_id: str, *, access_context: dict[str, Any] | None = None) -> ConnectorDocument:
        raise NotImplementedError

    def health_check(self) -> dict[str, Any]:
        return {"connector_id": self.connector_id, "status": "unknown"}

    def close(self) -> None:
        """Release connector-owned resources, if any."""

