from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api import agent, audit, documents, domains, evaluations, evidences, feedback, human_reviews, projects, research, search, tasks, web_research
from app.auth.oidc import get_request_context, validate_auth_configuration
from app.db.schema import initialize_database
from app.db.session import SessionLocal, engine, get_db, validate_postgres_rls_runtime
from app.evaluation.store import import_reports
from app.retrieval.bm25 import get_bm25_store
from app.llm_client import get_llm_client
from app.config import settings
from app.observability.metrics import record_http_request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    validate_auth_configuration()
    if settings.environment == "production" and not settings.task_queue_enabled:
        raise RuntimeError("TASK_QUEUE_ENABLED must be true in production; use the durable outbox dispatcher and Celery workers")
    reasoning = get_llm_client()
    _app.state.llm_client = reasoning
    try:
        if settings.environment == "production" and settings.db_auto_migrate:
            raise RuntimeError("DB_AUTO_MIGRATE must be false in production; run alembic upgrade head before starting the API")
        if settings.db_auto_migrate:
            initialize_database(engine)
        else:
            with engine.connect() as connection:
                if not inspect(connection).has_table("alembic_version"):
                    raise RuntimeError("Database migrations are missing; run alembic upgrade head")
                version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
                if not version:
                    raise RuntimeError("Database migration version is missing")
        if settings.environment == "production":
            validate_postgres_rls_runtime(engine)
        with SessionLocal() as db:
            get_bm25_store(engine).rebuild(db)
            import_reports(db)
        yield
    finally:
        reasoning.close()
        get_llm_client.cache_clear()

app = FastAPI(
    title="EvidencePilot API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.environment == "production" else "/docs",
    redoc_url=None if settings.environment == "production" else "/redoc",
    openapi_url=None if settings.environment == "production" else "/openapi.json",
)
app.include_router(research.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(domains.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(tasks.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(documents.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(search.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(web_research.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(agent.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(feedback.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(evaluations.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(evidences.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])
app.include_router(human_reviews.router, prefix="/api/v1", dependencies=[Depends(get_request_context)])


@app.middleware("http")
async def collect_http_metrics(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        record_http_request(request.method, route_template, status_code, time.perf_counter() - started)


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics endpoint disabled")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.exception_handler(SQLAlchemyError)
async def database_error(_request: Request, _exc: SQLAlchemyError):
    # Never return database URLs, credentials, SQL text or user parameters.
    return JSONResponse(status_code=503, content={"detail": "Database unavailable", "error_category": "database_unavailable"})


@app.get("/api/v1/system")
def system_info(request: Request, _principal=Depends(get_request_context)):
    client = getattr(request.app.state, "llm_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Inference client not initialized")
    return {"mode": client.config.llm_mode, "model": client.model, "version": app.version}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok", "database": "ok"}
