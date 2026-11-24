"""Private inference API. Deploy behind TLS; never expose directly to tenants."""
from contextlib import asynccontextmanager
from threading import BoundedSemaphore

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
        raise HTTPException(429, "Inference capacity exhausted")
    try:
        return {"model": getattr(local, kind + "_model"),
                "revision": getattr(local, kind + "_revision"), "values": operation()}
    except Exception:
        raise HTTPException(503, "Inference unavailable") from None
    finally:
        slots.release()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/v1/embeddings", dependencies=[Depends(authenticate)])
def embeddings(body: EmbeddingRequest):
    return run("embedding", lambda: embedding.embed_documents(body.texts))

@app.post("/v1/rerank", dependencies=[Depends(authenticate)])
def rerank(body: RerankRequest):
    return run("reranker", lambda: reranker.score(body.query, [
        {"id": str(i), "text": text} for i, text in enumerate(body.texts)]))
