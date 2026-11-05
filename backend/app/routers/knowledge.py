from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..services.nl2sql import (
    list_example_pairs,
    purge_example_pairs,
    purge_example_pairs_by_filter,
)
from ..services.knowledge_sync import dataset_status, sync_all, sync_dataset
from ..store import list_retrieval_logs


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class PurgeExamplesRequest(BaseModel):
    question_contains: str = ""
    all: bool = False
    doc_names: list[str] = []


@router.get("/status")
def status() -> list[dict]:
    return dataset_status()


@router.post("/sync")
def sync() -> list[dict]:
    return sync_all()


@router.post("/rechunk")
def rechunk() -> list[dict]:
    """忽略内容变化，全量重传并重新分块（用于应用新的分块配置）。"""
    return sync_all(force=True)


@router.post("/sync/{dataset_name}")
def sync_one(dataset_name: str) -> dict:
    from ..config import settings

    kind = settings.knowledge_datasets.get(dataset_name)
    if not kind:
        return {"dataset": dataset_name, "error": "未知数据集"}
    return sync_dataset(dataset_name, kind)


@router.get("/retrievals")
def retrievals(limit: int = 20) -> list[dict]:
    return list_retrieval_logs(limit=limit)


@router.get("/example-pairs")
def example_pairs(limit: int = 50) -> list[dict]:
    return list_example_pairs(limit=limit)


@router.post("/example-pairs/purge")
def purge_examples(payload: PurgeExamplesRequest) -> dict:
    if payload.doc_names:
        return purge_example_pairs(payload.doc_names)
    return purge_example_pairs_by_filter(
        question_contains=payload.question_contains,
        purge_all=payload.all,
    )
