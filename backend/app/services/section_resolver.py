from __future__ import annotations

import re
import time
from typing import Any

from ..config import settings
from . import text_match
from .db import db_manager
from .logging_service import log_error


_CACHE: tuple[float, list[dict[str, str]]] | None = None
_TTL = float(settings.reference_data_ttl_seconds)


def rows() -> list[dict[str, str]]:
    global _CACHE
    now = time.time()
    if _CACHE and now - _CACHE[0] < _TTL:
        return _CACHE[1]
    try:
        data = db_manager.query(
            "mysql",
            "SELECT id, full_name, code, route_id, section_version "
            "FROM basic_facilities_sections "
            "WHERE del_flag = 0",
        )["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("section_resolver", "section_rows_failed", exc)
        return _CACHE[1] if _CACHE else []
    out = [
        {
            "id": str(row.get("id") or ""),
            "full_name": str(row.get("full_name") or ""),
            "code": str(row.get("code") or ""),
            "route_id": str(row.get("route_id") or ""),
            "section_version": str(row.get("section_version") or ""),
        }
        for row in data
        if row.get("id")
    ]
    _CACHE = (now, out)
    return out


def resolve(mention: str) -> dict[str, Any]:
    query = str(mention or "").strip()
    matches = text_match.rank_rows(query, rows(), ("full_name", "code"))
    if not matches:
        return {"id": "", "name": "", "matches": [], "token": query}
    best = text_match.unique_best(matches)
    if best is None:
        return {
            "id": "",
            "name": "",
            "best": matches[0],
            "matches": matches,
            "unique": False,
            "token": query or str(matches[0].get("matched_name") or ""),
        }
    return {
        "id": str(best["id"]),
        "name": str(best["full_name"]),
        "best": best,
        "matches": matches,
        "unique": True,
        "token": query or str(best.get("matched_name") or ""),
    }


def mentions_from_message(message: str, max_len: int = 12) -> list[str]:
    """从问句里找像现有路段名的片段；匹配依据是数据库名字本身，不是业务关键词表。"""
    text = str(message or "")
    owners: dict[str, set[str]] = {}
    for row in rows():
        for key in ("full_name", "code"):
            name = str(row.get(key) or "").strip()
            if len(name) < 2:
                continue
            for size in range(min(max_len, len(name)), 1, -1):
                for start in range(0, len(name) - size + 1):
                    piece = name[start : start + size]
                    if piece in text:
                        owners.setdefault(piece, set()).add(str(row["id"]))
    # 最长优先，且同一批命中保留；纯数字/编码碎片要求至少 4 位。
    result: list[str] = []
    for piece in sorted(owners, key=lambda item: (-len(item), item)):
        if re.fullmatch(r"[0-9A-Za-z_\-]+", piece) and len(piece) < 4:
            continue
        result.append(piece)
        if len(result) >= 12:
            break
    return result


def resolve_message(message: str) -> dict[str, Any]:
    for token in mentions_from_message(message):
        hit = resolve(token)
        if hit.get("matches"):
            return hit
    return {"id": "", "name": "", "matches": [], "token": ""}


def clear_cache() -> None:
    global _CACHE
    _CACHE = None
