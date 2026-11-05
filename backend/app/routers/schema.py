from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..services.db import DatabaseError
from ..services.schema_catalog import live_catalog
from ..services import mdl_sync


router = APIRouter(prefix="/api/schema", tags=["schema"])


def resolve_generator(name: str = ""):
    """按名字取生成器；空串用 .env 的 MDL_GENERATOR（默认 legacy）。"""
    from ..services import mdl_sync_official

    chosen = str(name or settings.mdl_generator or "legacy").strip().lower()
    if chosen not in {"legacy", "official"}:
        raise ValueError(f"不认识的语义层生成器：{name}（可选 legacy / official）")
    return chosen, (mdl_sync_official if chosen == "official" else mdl_sync)


@router.get("")
def schema(kind: str = "mysql", include_rows: bool = False) -> dict:
    """实时读取数据库结构：表、字段、类型、注释。"""
    try:
        return live_catalog(kind, include_rows=include_rows)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatabaseError as exc:
        raise HTTPException(status_code=503, detail=f"读取数据库结构失败：{exc}") from exc


@router.get("/mdl/generators")
def mdl_generator_options() -> dict:
    """当前默认生成器 + 可选清单（前端"生成器"下拉用它渲染）。"""
    return {
        "default": str(settings.mdl_generator or "legacy"),
        "options": [
            {
                "value": "legacy",
                "label": "经典（手写类型字典）",
                "note": "原 mdl_sync.py：information_schema + 手写类型归族，类型抹平长度/精度",
            },
            {
                "value": "official",
                "label": "官方流程版",
                "note": "wren utils parse-types 归一类型 + validate 再 build + 外键关系草稿",
            },
        ],
    }


@router.post("/mdl/sync")
def sync_mdl(kind: str = "", generator: str = "") -> dict:
    """按实时库结构重新生成 Wren MDL（wren_projects/<项目>/models）并重新编译。

    表结构变了（加列/改列名/改类型）就在这里点一下，不用手改 metadata.yml。
    人工维护的语义（overrides.yml / relationships.yml / cubes）不受影响。

    generator：legacy / official 两套生成器可选（留空用 .env 的 MDL_GENERATOR）——
    两套输出可以 A/B 对比：官方版保留类型长度/精度（VARCHAR(32)、DECIMAL(16,2)、JSONB…），
    经典版把它们抹平；官方版还会先 validate 再 build，并生成外键关系草稿。
    """
    try:
        chosen, module = resolve_generator(generator)
        results = (
            [module.sync_project(kind, build=True)]
            if kind
            else module.sync_all(build=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatabaseError as exc:
        raise HTTPException(status_code=503, detail=f"读取数据库结构失败：{exc}") from exc
    # official 版可能先卡在 validate：这时如实报出来，别只说 build
    invalid = [item for item in results if item.get("valid") is False]
    if invalid:
        raise HTTPException(
            status_code=502,
            detail="MDL 已生成，但 wren context validate 没过："
            + "；".join(str(item.get("validate_output", ""))[:300] for item in invalid),
        )
    failed = [item for item in results if not item.get("built")]
    if failed:
        raise HTTPException(
            status_code=502,
            detail="MDL 已生成，但 wren context build 失败："
            + "；".join(item.get("build_output", "")[:200] for item in failed),
        )
    return {"generator": chosen, "synced": results}
