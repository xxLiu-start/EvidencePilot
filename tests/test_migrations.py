from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_baseline_creates_schema_and_tenant_migration_backfills_legacy_rows(tmp_path):
    database = tmp_path / "migration-test.sqlite"
    url = f"sqlite:///{database}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "20261123_0001")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "research_tasks" in tables
        assert "outbox_events" in tables
        assert "evidence_assessments" in tables
        assert "tenants" in tables
        assert "projects" in tables
        columns = {column["name"] for column in inspector.get_columns("research_tasks")}
        assert "domain_id" in columns
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO research_tasks (id, query, status, budget, domain_id, created_at) "
                "VALUES ('00000000-0000-0000-0000-000000000001', 'migration smoke test', 'queued', 2, "
                "'general_research', CURRENT_TIMESTAMP)"
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT tenant_id, project_id FROM research_tasks "
                "WHERE id = '00000000-0000-0000-0000-000000000001'"
            ).one()
            count = connection.exec_driver_sql("SELECT COUNT(*) FROM research_tasks").scalar_one()
            tenant_count = connection.exec_driver_sql("SELECT COUNT(*) FROM tenants").scalar_one()
            project_count = connection.exec_driver_sql("SELECT COUNT(*) FROM projects").scalar_one()
        assert count == 1
        assert row.tenant_id is not None and row.project_id is not None
        assert tenant_count == 1 and project_count == 1
    finally:
        engine.dispose()
