"""Read-only local file connector restricted to an explicitly configured root."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.connectors.base import ConnectorDocument, SourceConnector
from app.retrieval.ingestion import SUPPORTED_SUFFIXES, parse_document


class FileConnector(SourceConnector):
    connector_id = "local_files"

    def __init__(self, root: str | Path, *, max_file_bytes: int = 20_000_000) -> None:
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("File connector root must be a directory")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.max_file_bytes = max_file_bytes

    def _resolve(self, source_id: str) -> Path:
        candidate = (self.root / source_id).resolve(strict=True)
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise PermissionError("File is outside the configured connector root") from None
        if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError("Unsupported or non-file source")
        if candidate.stat().st_size > self.max_file_bytes:
            raise ValueError("File exceeds connector size limit")
        return candidate

    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None,
               access_context: dict[str, Any] | None = None) -> list[ConnectorDocument]:
        if not query.strip() or not 1 <= limit <= 100:
            raise ValueError("query and limit must be valid")
        suffixes = set((filters or {}).get("suffixes", SUPPORTED_SUFFIXES))
        matches = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            try:
                relative = path.resolve(strict=True).relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            if query.casefold() in path.name.casefold() or query.casefold() in relative.casefold():
                matches.append(ConnectorDocument(
                    source_id=relative, connector_id=self.connector_id, source_type="document",
                    canonical_location=relative, title=path.name,
                    retrieved_at=datetime.now(timezone.utc), metadata={"suffix": path.suffix.lower()},
                ))
                if len(matches) >= limit:
                    break
        return matches

    def fetch(self, source_id: str, *, access_context: dict[str, Any] | None = None) -> ConnectorDocument:
        path = self._resolve(source_id)
        content = parse_document(str(path))
        return ConnectorDocument(
            source_id=path.relative_to(self.root).as_posix(), connector_id=self.connector_id,
            source_type="document", canonical_location=path.relative_to(self.root).as_posix(),
            title=path.name, content=content, content_hash=sha256(content.encode("utf-8")).hexdigest(),
            retrieved_at=datetime.now(timezone.utc), metadata={"suffix": path.suffix.lower(), "bytes": path.stat().st_size},
        )

    def health_check(self) -> dict[str, Any]:
        return {"connector_id": self.connector_id, "status": "ok" if self.root.is_dir() else "unavailable"}
