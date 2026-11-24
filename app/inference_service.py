"""Private inference API. Deploy behind TLS; never expose directly to tenants."""
from contextlib import asynccontextmanager
import hmac
from threading import BoundedSemaphore
from time import monotonic
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response, JSONResponse
from fastapi.exceptions import RequestValidationError

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
        metric_token = local.inference_metrics_token.get_secret_value()
        if len(metric_token) < 32 or metric_token == local.inference_token.get_secret_value():
            raise RuntimeError("Separate inference metrics credential required")
        for kind in ("embedding", "reranker"):
            verified_model_path(local, kind)
    yield

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Pydantic validation errors can contain the original confidential input.
    return JSONResponse(status_code=422, content={"detail": "Invalid inference request"})

class BodyLimit:
    """Limit body bytes before parsing, including chunked requests."""
    def __init__(self, app, limit=4_000_000):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                return await JSONResponse(status_code=413, content={"detail": "Request too large"})(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}
        return await self.app(scope, replay, send)

app.add_middleware(BodyLimit)

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

def authenticate_metrics(authorization: Annotated[str | None, Header()] = None):
    token = local.inference_metrics_token.get_secret_value()
    if len(token) < 32 or not hmac.compare_digest((authorization or "").encode(), ("Bearer " + token).encode()):
        raise HTTPException(401, "Unauthorized")

@app.get("/metrics", dependencies=[Depends(authenticate_metrics)])
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
