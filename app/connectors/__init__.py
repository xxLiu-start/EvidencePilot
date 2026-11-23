"""Pluggable, permission-aware source connector contracts."""

from app.connectors.base import ConnectorDocument, SourceConnector
from app.connectors.registry import ConnectorRegistry
from app.connectors.files import FileConnector
from app.connectors.web import WebSearchConnector

__all__ = ["ConnectorDocument", "SourceConnector", "ConnectorRegistry", "FileConnector", "WebSearchConnector"]
