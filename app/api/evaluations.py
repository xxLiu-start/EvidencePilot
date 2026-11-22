from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db.models import EvaluationResult
from app.db.session import get_db

router = APIRouter()


@router.get("/evaluations")
def get_evaluations(db: Annotated[Session, Depends(get_db)], limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = db.scalars(select(EvaluationResult).order_by(EvaluationResult.created_at.desc(), EvaluationResult.id).offset(offset).limit(limit))
    return {"items": [{"id": row.id, "name": row.name, "content_hash": row.content_hash,
                       "result": row.result, "created_at": row.created_at} for row in rows],
            "total": db.scalar(select(func.count()).select_from(EvaluationResult)), "limit": limit, "offset": offset}
