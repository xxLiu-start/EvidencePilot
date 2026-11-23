"""Add bounded outbox retry scheduling and task ownership constraint."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20261123_0002"
down_revision = "20261123_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("outbox_events")}
    if "next_attempt_at" not in columns:
        op.add_column("outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))

    if bind.dialect.name == "postgresql":
        foreign_keys = inspector.get_foreign_keys("outbox_events")
        has_task_fk = any(item.get("referred_table") == "research_tasks"
                          and item.get("constrained_columns") == ["aggregate_id"]
                          for item in foreign_keys)
        if not has_task_fk:
            op.create_foreign_key(
                "fk_outbox_events_aggregate_id_research_tasks",
                "outbox_events",
                "research_tasks",
                ["aggregate_id"],
                ["id"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    # Additive outbox metadata is retained to avoid deleting retry/audit state.
    pass
