"""PostgreSQL transaction-local security context for forced row-level security."""
from __future__ import annotations

from sqlalchemy import text


_RLS_SETTINGS_SQL = text("""
SELECT
  set_config('app.tenant_id', :tenant_id, true),
  set_config('app.project_ids', :project_ids, true),
  set_config('app.user_id', :user_id, true),
  set_config('app.identity_issuer', :identity_issuer, true),
  set_config('app.identity_subject', :identity_subject, true),
  set_config('app.tenant_external_id', :tenant_external_id, true),
  set_config('app.tenant_admin', :tenant_admin, true),
  set_config('app.bootstrap_task_id', :bootstrap_task_id, true)
""")


def _string(value) -> str:
    return "" if value is None else str(value)


def apply_rls_context(connection, info: dict) -> None:
    """Set transaction-local PostgreSQL GUCs; SQLite remains unaffected.

    GUCs are re-applied after every transaction begins. Empty values are
    deliberately fail-closed in RLS policies.
    """
    if connection.dialect.name != "postgresql":
        return
    project_ids = info.get("selected_project_ids") or info.get("project_ids") or ()
    values = {
        "tenant_id": _string(info.get("tenant_id")),
        "project_ids": ",".join(sorted(str(value) for value in project_ids)),
        "user_id": _string(info.get("user_id")),
        "identity_issuer": _string(info.get("identity_issuer")),
        "identity_subject": _string(info.get("identity_subject")),
        "tenant_external_id": _string(info.get("tenant_external_id")),
        "tenant_admin": "true" if info.get("tenant_role") in {"TENANT_ADMIN", "SUPER_ADMIN"} else "false",
        "bootstrap_task_id": _string(info.get("bootstrap_task_id")),
    }
    connection.execute(_RLS_SETTINGS_SQL, values)


def set_session_scope(db, *, tenant_id, user_id=None, project_ids=(),
                      selected_project_id=None, tenant_role=None,
                      identity_issuer=None, identity_subject=None,
                      tenant_external_id=None) -> None:
    """Update the current Session and the active transaction's RLS context."""
    db.info.update({
        "tenant_id": tenant_id,
        "user_id": user_id,
        "project_ids": tuple(project_ids or ()),
        "selected_project_id": selected_project_id,
        "selected_project_ids": (selected_project_id,) if selected_project_id else tuple(project_ids or ()),
        "tenant_role": tenant_role,
        "identity_issuer": identity_issuer if identity_issuer is not None else db.info.get("identity_issuer"),
        "identity_subject": identity_subject if identity_subject is not None else db.info.get("identity_subject"),
        "tenant_external_id": tenant_external_id if tenant_external_id is not None else db.info.get("tenant_external_id"),
    })
    apply_rls_context(db.connection(), db.info)
