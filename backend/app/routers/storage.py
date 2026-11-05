from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..services.db import DatabaseError, db_manager
from ..services.schema_catalog import live_catalog


router = APIRouter(prefix="/api/storage", tags=["storage"])

# 指向对象存储的字段名特征（按库里的真实字段名匹配，不写死表）
_REFERENCE_KEYWORDS = ("url", "pic", "image", "photo", "path", "file")


@router.get("")
def browse(path: str = "buckets") -> dict:
    """浏览 SeaweedFS 目录（默认列出所有桶）。"""
    try:
        entries = db_manager.list_seaweedfs(path)
    except DatabaseError as exc:
        raise HTTPException(status_code=503, detail=f"读取 SeaweedFS 失败：{exc}") from exc
    return {"path": path.strip("/"), "entries": entries}


@router.get("/references")
def references() -> list[dict]:
    """从数据库结构里发现指向对象存储的字段，并统计非空行数。"""
    try:
        catalog = live_catalog("mysql", include_rows=False)
    except DatabaseError as exc:
        raise HTTPException(status_code=503, detail=f"读取数据库结构失败：{exc}") from exc

    hits: list[dict] = []
    for table in catalog.get("tables", []):
        for column in table.get("columns", []):
            name = str(column.get("name") or "")
            if not any(keyword in name.lower() for keyword in _REFERENCE_KEYWORDS):
                continue
            hits.append(
                {
                    "table": table["table"],
                    "table_comment": table.get("comment"),
                    "column": name,
                    "type": column.get("type"),
                    "comment": column.get("comment"),
                    "filled": None,
                    "total": None,
                }
            )

    if hits:
        # 合并成一条 UNION ALL，避免 20 多次 SSH 往返
        parts = [
            f"SELECT '{item['table']}.{item['column']}' AS col, COUNT(*) AS total, "
            f"SUM(CASE WHEN `{item['column']}` IS NOT NULL AND `{item['column']}` <> '' "
            f"THEN 1 ELSE 0 END) AS filled FROM `{item['table']}`"
            for item in hits
        ]
        try:
            rows = db_manager.query("mysql", " UNION ALL ".join(parts))["rows"]
            counts = {str(row.get("col")): row for row in rows}
            for item in hits:
                row = counts.get(f"{item['table']}.{item['column']}")
                if row:
                    item["total"] = row.get("total")
                    item["filled"] = row.get("filled")
        except Exception:
            pass
    return hits
