"""Tenant-isolated SQL caches; preserve attributable evidence, quarantine global history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20261124_0007"
down_revision = "20261123_0006"
branch_labels = None
depends_on = None

CACHE_TABLES = ("claims", "web_contents", "web_sources", "web_searches")
DEPENDENTS = ("evidences", "research_tasks", "agent_runs", "agent_allowed_sources")


def _drop_fk(bind, table, referred):
    for item in inspect(bind).get_foreign_keys(table):
        if item["referred_table"] in referred:
            op.drop_constraint(item["name"], table, type_="foreignkey")


def _drop_unique(bind, table, columns):
    for item in inspect(bind).get_unique_constraints(table):
        if set(item["column_names"]) == set(columns):
            op.drop_constraint(item["name"], table, type_="unique")


def _upgrade_legacy(bind):
    # The old schema stored global content and queries. Only reconstruct rows
    # attributable through tenant-owned evidence or task-authorized sources.
    # Ambiguous legacy search history remains offline, invisible to runtime.
    for table in DEPENDENTS:
        bind.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    for table in CACHE_TABLES + ("evidences",):
        op.add_column(table, sa.Column("cache_scope", sa.String(36), nullable=False,
                                      server_default="offline"))
        if table != "web_contents":
            op.create_index(f"ix_{table}_cache_scope", table, ["cache_scope"])

    _drop_fk(bind, "evidences", {"claims", "web_contents", "web_sources"})
    _drop_fk(bind, "web_searches", {"claims"})
    _drop_fk(bind, "web_sources", {"web_contents"})
    _drop_unique(bind, "claims", ["claim_id", "claim_text"])
    _drop_unique(bind, "web_sources", ["canonical_url"])
    op.drop_constraint(inspect(bind).get_pk_constraint("web_contents")["name"],
                       "web_contents", type_="primary")
    op.create_primary_key("web_contents_pkey", "web_contents", ["cache_scope", "content_hash"])
    op.create_unique_constraint("uq_claim_scope_text", "claims", ["cache_scope", "claim_id", "claim_text"])
    op.create_unique_constraint("uq_claim_scope_id", "claims", ["cache_scope", "id"])
    op.create_unique_constraint("uq_web_source_scope_url", "web_sources", ["cache_scope", "canonical_url"])

    bind.execute(sa.text("""
        CREATE TEMP TABLE ep_cache_source_owners ON COMMIT DROP AS
        SELECT DISTINCT ON (e.tenant_id, e.source_url)
               e.tenant_id::text AS cache_scope, s.id AS source_id,
               e.content_hash, e.published_at, e.retrieved_at
          FROM evidences e JOIN web_sources s ON s.canonical_url=e.source_url
         WHERE e.tenant_id IS NOT NULL AND s.cache_scope='offline'
         ORDER BY e.tenant_id, e.source_url, e.retrieved_at DESC, e.id
    """))
    bind.execute(sa.text("""
        CREATE TEMP TABLE ep_cache_claim_owners ON COMMIT DROP AS
        SELECT DISTINCT e.tenant_id::text AS cache_scope, e.claim_record_id AS claim_id
          FROM evidences e WHERE e.tenant_id IS NOT NULL
        UNION
        SELECT DISTINCT t.tenant_id::text, c.id
          FROM agent_runs r JOIN research_tasks t ON t.id=r.task_id
          CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(r.state::jsonb->'claims')='array'
                 THEN r.state::jsonb->'claims' ELSE '[]'::jsonb END
          ) AS item
          JOIN claims c ON c.claim_id=item->>'id' AND c.claim_text=item->>'claim'
         WHERE t.tenant_id IS NOT NULL AND c.cache_scope='offline'
    """))
    bind.execute(sa.text("""
        INSERT INTO web_contents (cache_scope, content_hash, body, cache_path)
        SELECT e.tenant_id::text, c.content_hash, c.body, ''
          FROM evidences e JOIN web_contents c ON c.content_hash=e.content_hash
         WHERE e.tenant_id IS NOT NULL AND c.cache_scope='offline'
        ON CONFLICT (cache_scope, content_hash) DO NOTHING
    """))
    bind.execute(sa.text("""
        INSERT INTO claims (cache_scope, id, claim_id, claim_text)
        SELECT o.cache_scope, gen_random_uuid(), c.claim_id, c.claim_text
          FROM ep_cache_claim_owners o JOIN claims c ON c.id=o.claim_id
        ON CONFLICT (cache_scope, claim_id, claim_text) DO NOTHING
    """))
    # Do not copy mutable global URL metadata or the latest global body.
    # Only reconstruct a source from the content hash and dates attributed
    # to a persisted tenant-owned evidence record. Old allowed-source links
    # without evidence remain inaccessible and must be searched again.
    bind.execute(sa.text("""
        INSERT INTO web_sources (cache_scope, id, canonical_url, fetched_url, title,
                                 content_hash, published_at, retrieved_at, source_type, search_queries)
        SELECT o.cache_scope, gen_random_uuid(), s.canonical_url, s.canonical_url, s.canonical_url,
               o.content_hash, o.published_at, o.retrieved_at, 'web', '[]'::json
          FROM ep_cache_source_owners o JOIN web_sources s ON s.id=o.source_id
        ON CONFLICT (cache_scope, canonical_url) DO NOTHING
    """))
    bind.execute(sa.text("""
        UPDATE evidences e SET cache_scope=e.tenant_id::text, claim_record_id=new.id
          FROM claims old JOIN claims new
            ON new.claim_id=old.claim_id AND new.claim_text=old.claim_text
         WHERE e.claim_record_id=old.id AND old.cache_scope='offline'
           AND new.cache_scope=e.tenant_id::text
    """))
    bind.execute(sa.text("""
        UPDATE agent_allowed_sources a SET source_id=new.id
          FROM research_tasks t, web_sources old, web_sources new
         WHERE t.id=a.task_id AND old.id=a.source_id AND old.cache_scope='offline'
           AND new.cache_scope=t.tenant_id::text AND new.canonical_url=old.canonical_url
    """))
    op.create_foreign_key("fk_web_sources_scoped_content", "web_sources", "web_contents",
                          ["cache_scope", "content_hash"], ["cache_scope", "content_hash"])
    op.create_foreign_key("fk_web_searches_scoped_claim", "web_searches", "claims",
                          ["cache_scope", "claim_record_id"], ["cache_scope", "id"])
    op.create_foreign_key("fk_evidence_scoped_claim", "evidences", "claims",
                          ["cache_scope", "claim_record_id"], ["cache_scope", "id"])
    op.create_foreign_key("fk_evidence_scoped_content", "evidences", "web_contents",
                          ["cache_scope", "content_hash"], ["cache_scope", "content_hash"])
    op.create_foreign_key("fk_evidence_scoped_source", "evidences", "web_sources",
                          ["cache_scope", "source_url"], ["cache_scope", "canonical_url"])
    for table in DEPENDENTS:
        bind.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def upgrade():
    bind = op.get_bind()
    legacy = "cache_scope" not in {c["name"] for c in inspect(bind).get_columns("web_contents")}
    if bind.dialect.name != "postgresql":
        if legacy:
            raise RuntimeError("Legacy SQLite cache migration is unsupported; export data and create a fresh database")
        return
    if legacy:
        _upgrade_legacy(bind)
    # An evidence reference may not be pointed at another tenant's cache key
    # even if the caller uses direct SQL rather than the application ORM.
    op.create_check_constraint("ck_evidence_cache_scope_tenant", "evidences",
                               "cache_scope = tenant_id::text")
    for table in CACHE_TABLES:
        bind.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        predicate = "cache_scope = NULLIF(current_setting('app.tenant_id', true), '')"
        bind.execute(sa.text(
            f"CREATE POLICY {table}_tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        ))


def downgrade():
    # A rollback that restores global keys would re-expose tenant content.
    raise RuntimeError("Tenant cache isolation cannot be downgraded; restore a controlled pre-migration backup")
