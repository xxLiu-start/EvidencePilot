"""Add tenant-scoped human review audit records."""
from alembic import op
from sqlalchemy import text

from app.db.session import Base
from app.db import models  # noqa: F401


revision = "20261123_0006"
down_revision = "20261123_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    if bind.dialect.name == "postgresql":
        bind.execute(text("ALTER TABLE human_reviews ENABLE ROW LEVEL SECURITY"))
        bind.execute(text("ALTER TABLE human_reviews FORCE ROW LEVEL SECURITY"))
        bind.execute(text("DROP POLICY IF EXISTS human_reviews_tenant_isolation ON human_reviews"))
        bind.execute(text(
            "CREATE POLICY human_reviews_tenant_isolation ON human_reviews "
            "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
        ))


def downgrade() -> None:
    # Preserve reviewer decisions and audit history across application rollback.
    pass
