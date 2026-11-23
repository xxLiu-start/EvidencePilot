from sqlalchemy import create_engine, event, false, or_
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session, with_loader_criteria

from app.config import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_project_filters(execute_state):
    """Apply fail-closed tenant/project filters to every ORM SELECT in scoped sessions."""
    if not execute_state.is_select:
        return
    db = execute_state.session
    tenant_id = db.info.get("tenant_id")
    if tenant_id is None:
        return
    statement = execute_state.statement
    project_ids = db.info.get("selected_project_ids") or db.info.get("project_ids") or ()
    for mapper in Base.registry.mappers:
        model = mapper.class_
        columns = mapper.columns
        if "tenant_id" in columns:
            tenant_filter = model.tenant_id == tenant_id
            if model.__name__ == "EvaluationResult":
                tenant_filter = or_(model.tenant_id.is_(None), tenant_filter)
            statement = statement.options(
                with_loader_criteria(model, tenant_filter, include_aliases=True)
            )
        if "project_id" in columns:
            if project_ids:
                project_filter = model.project_id.in_(tuple(project_ids))
                if columns["project_id"].nullable:
                    project_filter = or_(model.project_id.is_(None), project_filter)
                statement = statement.options(
                    with_loader_criteria(model, project_filter, include_aliases=True)
                )
            else:
                project_filter = model.project_id.is_(None) if columns["project_id"].nullable else false()
                statement = statement.options(
                    with_loader_criteria(model, project_filter, include_aliases=True)
                )
    execute_state.statement = statement


@event.listens_for(Session, "before_flush")
def _enforce_tenant_project_writes(db, flush_context, instances):
    """Stamp tenant ownership and reject cross-tenant/project writes."""
    tenant_id = db.info.get("tenant_id")
    if tenant_id is None:
        return
    project_ids = set(db.info.get("project_ids") or ())
    selected_project_id = db.info.get("selected_project_id")
    user_id = db.info.get("user_id")
    for obj in db.new.union(db.dirty):
        mapper = getattr(obj, "__mapper__", None)
        if mapper is None:
            continue
        columns = mapper.columns
        if "tenant_id" in columns:
            current_tenant = getattr(obj, "tenant_id", None)
            if current_tenant is None:
                setattr(obj, "tenant_id", tenant_id)
            elif current_tenant != tenant_id:
                raise PermissionError("Cross-tenant write rejected")
        if "project_id" in columns:
            current_project = getattr(obj, "project_id", None)
            if current_project is None:
                if selected_project_id is not None:
                    current_project = selected_project_id
                elif len(project_ids) == 1:
                    current_project = next(iter(project_ids))
                elif not columns["project_id"].nullable:
                    raise ValueError("X-Project-ID is required when the user belongs to multiple projects")
                if current_project is not None:
                    setattr(obj, "project_id", current_project)
            if current_project is not None and current_project not in project_ids:
                raise PermissionError("Project membership required")
        if "created_by_user_id" in columns and getattr(obj, "created_by_user_id", None) is None:
            setattr(obj, "created_by_user_id", user_id)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
