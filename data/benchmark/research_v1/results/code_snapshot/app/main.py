from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import agent, documents, evaluations, feedback, research, search, tasks, web_research
from app.db.schema import initialize_database
from app.db.session import SessionLocal, engine, get_db
from app.evaluation.store import import_reports
from app.retrieval.bm25 import get_bm25_store
from app.llm_client import get_llm_client


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    reasoning = get_llm_client()
    _app.state.llm_client = reasoning
    try:
        initialize_database(engine)
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
)
app.include_router(research.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")
app.include_router(web_research.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(feedback.router, prefix="/api/v1")
app.include_router(evaluations.router, prefix="/api/v1")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok", "database": "ok"}
