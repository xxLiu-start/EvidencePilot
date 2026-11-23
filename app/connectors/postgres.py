"""Read-only PostgreSQL table connector with explicit column and tenant filters."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from sqlalchemy import MetaData, String, Table, cast, or_, select, text
from sqlalchemy.engine import Engine

from app.connectors.base import ConnectorDocument, SourceConnector


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgreSQLTableConnector(SourceConnector):
    """Search/fetch rows from one explicitly configured table.

    No user-provided SQL is executed. For tenant-owned tables, tenant_column is
    mandatory and every request must provide tenant_id in access_context. Use a
    database role that has SELECT-only privileges as defense in depth.
    """

    connector_id = "postgresql_table"

    def __init__(
        self,
        engine: Engine,
        *,
        table_name: str,
        primary_key: str,
        searchable_columns: list[str],
        tenant_column: str | None = None,
        filter_columns: list[str] | None = None,
        schema: str | None = None,
        max_rows: int = 20,
    ) -> None:
        if not _IDENTIFIER.fullmatch(table_name) or (schema and not _IDENTIFIER.fullmatch(schema)):
            raise ValueError("Invalid table or schema identifier")
        if not _IDENTIFIER.fullmatch(primary_key):
            raise ValueError("Invalid primary-key identifier")
        if not 1 <= max_rows <= 100:
            raise ValueError("max_rows must be between 1 and 100")
        self.engine = engine
        self.table = Table(table_name, MetaData(), schema=schema, autoload_with=engine)
        self.primary_key = primary_key
        self.searchable_columns = tuple(searchable_columns)
        self.tenant_column = tenant_column
        self.filter_columns = tuple(filter_columns or ())
        self.max_rows = max_rows
        allowed = set(self.table.c.keys())
        configured = {primary_key, *self.searchable_columns, *self.filter_columns}
        if tenant_column:
            configured.add(tenant_column)
        if not self.searchable_columns or not configured <= allowed:
            raise ValueError("Configured connector columns must exist in the selected table")
        if len(self.table.primary_key.columns) != 1 or self.table.primary_key.columns[0].name != primary_key:
            raise ValueError("Configured primary key must match the table's single-column primary key")

    def _tenant_clause(self, access_context: dict[str, Any] | None):
        if not self.tenant_column:
            return None
        tenant_id = (access_context or {}).get("tenant_id")
        if not isinstance(tenant_id, (str, int)) or not str(tenant_id).strip():
            raise PermissionError("Tenant context is required for this connector")
        return self.table.c[self.tenant_column] == tenant_id

    def _read(self, statement):
        with self.engine.connect() as connection:
            with connection.begin():
                if self.engine.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                return connection.execute(statement).mappings().all()

    @staticmethod
    def _document(source_id: str, connector_id: str, location: str, row: dict[str, Any],
                  access_context: dict[str, Any] | None) -> ConnectorDocument:
        content = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        return ConnectorDocument(
            source_id=source_id,
            connector_id=connector_id,
            source_type="database",
            canonical_location=location,
            title=str(row.get("title") or row.get("name") or location)[:500],
            content=content,
            content_hash=sha256(content.encode("utf-8")).hexdigest(),
            retrieved_at=datetime.now(timezone.utc),
            authority_level="internal",
            access_context={"tenant_id": (access_context or {}).get("tenant_id")},
            metadata={"record": row},
        )

    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None,
               access_context: dict[str, Any] | None = None) -> list[ConnectorDocument]:
        if not query.strip() or not 1 <= limit <= self.max_rows:
            raise ValueError("query and limit must be valid")
        clauses = [cast(self.table.c[name], String).ilike(f"%{query.strip()}%")
                   for name in self.searchable_columns]
        predicates = [or_(*clauses)]
        tenant_clause = self._tenant_clause(access_context)
        if tenant_clause is not None:
            predicates.append(tenant_clause)
        for key, value in (filters or {}).items():
            if key not in self.filter_columns:
                raise ValueError(f"Filter column is not allowed: {key}")
            predicates.append(self.table.c[key] == value)
        statement = select(self.table).where(*predicates).limit(limit)
        rows = self._read(statement)
        return [self._document(str(row[self.primary_key]), self.connector_id,
                               f"{self.table.fullname}/{row[self.primary_key]}", dict(row), access_context)
                for row in rows]

    def fetch(self, source_id: str, *, access_context: dict[str, Any] | None = None) -> ConnectorDocument:
        predicates = [self.table.c[self.primary_key] == source_id]
        tenant_clause = self._tenant_clause(access_context)
        if tenant_clause is not None:
            predicates.append(tenant_clause)
        rows = self._read(select(self.table).where(*predicates).limit(1))
        if not rows:
            # Do not reveal whether a row exists outside the caller's tenant.
            raise LookupError("Database source not found")
        row = dict(rows[0])
        return self._document(str(row[self.primary_key]), self.connector_id,
                              f"{self.table.fullname}/{row[self.primary_key]}", row, access_context)

    def health_check(self) -> dict[str, Any]:
        try:
            self._read(select(text("1")))
            return {"connector_id": self.connector_id, "status": "ok"}
        except Exception:
            return {"connector_id": self.connector_id, "status": "unavailable"}
