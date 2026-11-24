"""Private inference API. Deploy behind TLS; never expose directly to tenants."""
from contextlib import asynccontextmanager
from threading import BoundedSemaphore
from time import monotonic
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response, JSONResponse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

from app.config import settings
from app.retrieval.embedding import BGEEmbeddingService
from app.retrieval.reranker import RerankerService
from app.retrieval.remote import authorized
from app.retrieval.model_integrity import verified_model_path

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Texts = Annotated[list[Text], Field(min_length=1, max_length=32)]

class EmbeddingRequest(BaseModel):
    texts: Texts

class RerankRequest(EmbeddingRequest):
    query: Text

local = settings.model_copy(update={"inference_mode": "local"})
embedding = BGEEmbeddingService(local)
reranker = RerankerService(local)
slots = BoundedSemaphore(1)
REQUESTS = Counter("evidencepilot_inference_requests_total", "Inference outcomes", ["kind", "outcome"])
DURATION = Histogram("evidencepilot_inference_duration_seconds", "Inference execution duration", ["kind"],
                     buckets=(.01,.05,.1,.25,.5,1,2.5,5,10,20,30,60,120))
ACTIVE = Gauge("evidencepilot_inference_active", "Active inference operations")
BATCH = Histogram("evidencepilot_inference_batch_size", "Input batch sizes", ["kind"], buckets=(1,2,4,8,16,32))


@asynccontextmanager
async def lifespan(app):
    if len(local.inference_token.get_secret_value()) < 32:
        raise RuntimeError("Inference service token is required")
    if local.environment == "production":
        for kind in ("embedding", "reranker"):
            verified_model_path(local, kind)
    yield

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

def authenticate(authorization: Annotated[str | None, Header()] = None):
    if not authorized(local, authorization):
        raise HTTPException(401, "Unauthorized")

def run(kind, operation):
    if not slots.acquire(blocking=False):
        REQUESTS.labels(kind, "overloaded").inc()
        raise HTTPException(429, "Inference capacity exhausted", headers={"Retry-After": "1"})
    started = monotonic()
    ACTIVE.inc()
    try:
        values = operation()
        REQUESTS.labels(kind, "success").inc()
        return {"model": getattr(local, kind + "_model"),
                "revision": getattr(local, kind + "_revision"), "values": values}
    except Exception:
        REQUESTS.labels(kind, "error").inc()
        raise HTTPException(503, "Inference unavailable") from None
    finally:
        ACTIVE.dec()
        DURATION.labels(kind).observe(monotonic() - started)
        slots.release()

@app.get("/metrics", dependencies=[Depends(authenticate)])
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/v1/embeddings", dependencies=[Depends(authenticate)])
def embeddings(body: EmbeddingRequest):
    BATCH.labels("embedding").observe(len(body.texts))
    return run("embedding", lambda: embedding.embed_documents(body.texts))

@app.post("/v1/rerank", dependencies=[Depends(authenticate)])
def rerank(body: RerankRequest):
    BATCH.labels("reranker").observe(len(body.texts))
    return run("reranker", lambda: reranker.score(body.query, [
        {"id": str(i), "text": text} for i, text in enumerate(body.texts)]))
