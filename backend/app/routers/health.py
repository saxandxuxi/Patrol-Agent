from __future__ import annotations

from fastapi import APIRouter

from ..services.db import db_manager
from ..config import settings
from ..services.rag import RAGError, list_datasets


router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    rag_status = "unknown"
    try:
        list_datasets()
        rag_status = "ok"
    except RAGError:
        rag_status = "error"
    return {
        "status": "ok",
        "database": db_manager.health(),
        "ragflow": rag_status,
        "nl2sql_engine": settings.nl2sql_engine,
    }
