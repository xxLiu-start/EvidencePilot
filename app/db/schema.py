"""Create new tables and apply the additive BGE-M3 schema upgrade."""

from sqlalchemy import Engine, inspect, text

from app.db.session import Base
from app.retrieval.embedding import BGE_M3_DIMENSIONS


def initialize_database(engine: Engine) -> None:
    # Ensure tables have been registered even for standalone CLI callers.
    from app.db import models  # noqa: F401

    with engine.begin() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(connection)
        # Additive migration for pre-checkpoint deployments, including local SQLite.
        additions = {"evidences": {"scope": "JSON NOT NULL DEFAULT '{}'"},
                     "agent_runs": {"owner_token": "VARCHAR(64)",
                                    "lease_expires_at": "TIMESTAMP WITH TIME ZONE" if postgres else "DATETIME",
                                    "checkpoint_seq": "INTEGER NOT NULL DEFAULT 0"}}
        for table, fields in additions.items():
            columns = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in fields.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
        if postgres:
            # create_all does not alter a table created by a previous release.
            connection.execute(text(
                "ALTER TABLE document_chunks "
                f"ADD COLUMN IF NOT EXISTS embedding vector({BGE_M3_DIMENSIONS}), "
                "ADD COLUMN IF NOT EXISTS embedding_model text"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
                "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
                "WHERE embedding IS NOT NULL"
            ))
            connection.execute(text(
                "ALTER TABLE evidences ALTER COLUMN source_url DROP NOT NULL, "
                "ADD COLUMN IF NOT EXISTS chunk_id uuid REFERENCES document_chunks(id)"
            ))
