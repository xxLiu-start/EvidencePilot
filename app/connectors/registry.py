"""Explicit connector registry; no dynamic imports or model-controlled registration."""
from __future__ import annotations

from app.connectors.base import SourceConnector


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, SourceConnector] = {}

    def register(self, connector: SourceConnector, *, replace: bool = False) -> None:
        connector_id = getattr(connector, "connector_id", "")
        if not connector_id or not isinstance(connector_id, str):
            raise ValueError("Connector must declare a non-empty connector_id")
        if connector_id in self._connectors and not replace:
            raise ValueError(f"Connector already registered: {connector_id}")
        self._connectors[connector_id] = connector

    def get(self, connector_id: str) -> SourceConnector:
        try:
            return self._connectors[connector_id]
        except KeyError:
            raise LookupError(f"Unknown connector: {connector_id}") from None

    def list_connectors(self) -> list[dict[str, str]]:
        return [{"connector_id": key, "status": "registered"} for key in sorted(self._connectors)]

    def close(self) -> None:
        for connector in self._connectors.values():
            connector.close()
