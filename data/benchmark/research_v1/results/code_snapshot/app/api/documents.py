from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID
from zipfile import BadZipFile

from docx.opc.exceptions import PackageNotFoundError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from lxml.etree import XMLSyntaxError
from pypdf.errors import PdfReadError
from sqlalchemy.orm import Session

from app.api.schemas import ChunkResponse, DocumentResponse
from app.db.models import DocumentChunk, DocumentRecord
from app.db.repository import ingest_document, list_document_chunks
from app.db.session import get_db
from app.retrieval.chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from app.retrieval.ingestion import SUPPORTED_SUFFIXES
from app.retrieval.embedding import EmbeddingError


router = APIRouter()


@router.post("/documents", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
@router.post("/documents/upload", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
def upload_document(
    file: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
    source: Annotated[str | None, Form()] = None,
    version: Annotated[str, Form(min_length=1)] = "1",
    chunk_size: Annotated[int, Form(gt=0)] = DEFAULT_CHUNK_SIZE,
    chunk_overlap: Annotated[int, Form(ge=0)] = DEFAULT_CHUNK_OVERLAP,
) -> DocumentRecord:
    filename = Path((file.filename or "").replace("\\", "/")).name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=415, detail="Supported file types: .pdf, .docx, .md, .txt")
    if chunk_overlap >= chunk_size:
        raise HTTPException(status_code=422, detail="chunk_overlap must be less than chunk_size")
    if not version.strip() or (source is not None and not source.strip()):
        raise HTTPException(status_code=422, detail="source and version must be non-empty")
    try:
        with TemporaryDirectory(prefix="evidencepilot-upload-") as directory:
            path = Path(directory) / ("document" + suffix)
            with path.open("wb") as destination:
                shutil.copyfileobj(file.file, destination)
            return ingest_document(
                db, str(path), filename=filename,
                source=source.strip() if source is not None else filename,
                version=version.strip(), chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            )
    except EmbeddingError:
        raise HTTPException(
            status_code=503, detail="Embedding service unavailable; document was not saved"
        ) from None
    except (ValueError, UnicodeError, PdfReadError, BadZipFile, PackageNotFoundError, XMLSyntaxError):
        raise HTTPException(status_code=422, detail="Document is invalid or contains no extractable text") from None


@router.get("/documents")
def list_documents(db: Annotated[Session, Depends(get_db)], limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = db.scalars(select(DocumentRecord).order_by(DocumentRecord.created_at.desc(), DocumentRecord.id).offset(offset).limit(limit))
    return {"items": [DocumentResponse.model_validate(row) for row in rows],
            "total": db.scalar(select(func.count()).select_from(DocumentRecord)), "offset": offset, "limit": limit}


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkResponse])
def get_document_chunks(
    document_id: UUID, db: Annotated[Session, Depends(get_db)]
) -> list[DocumentChunk]:
    if db.get(DocumentRecord, document_id) is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return list_document_chunks(db, document_id)
