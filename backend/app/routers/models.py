from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.llm import LLMError, test_connection
from ..services import model_catalog
from ..store import delete_model_config, list_model_configs, save_model_config


router = APIRouter(prefix="/api/models", tags=["models"])


class ModelPayload(BaseModel):
    name: str
    base_url: str
    api_key: str
    model: str
    is_default: bool = False


@router.get("")
def get_models() -> list[dict]:
    return list_model_configs()


@router.get("/catalog")
def get_model_catalog() -> dict:
    return model_catalog.snapshot(auto_refresh=True)


@router.post("/catalog/refresh")
def refresh_model_catalog() -> dict:
    return model_catalog.refresh_now()


@router.post("")
def create_model(payload: ModelPayload) -> dict:
    return save_model_config(payload.model_dump())


@router.post("/test")
def test_model(payload: ModelPayload) -> dict:
    try:
        return test_connection(payload.model_dump())
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{model_id}")
def remove_model(model_id: str) -> dict:
    delete_model_config(model_id)
    return {"ok": True}
