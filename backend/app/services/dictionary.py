"""字典驱动：code→名称 一律从数据库列注释里解析，不再写死在代码里。

依据《写死Bug清单》2.6：巡查类型、病害大类、路面类型、技术等级这些映射
以前是硬编码的 CASE WHEN，字典一变就错。这里改成读 information_schema 的
COLUMN_COMMENT（形如"巡检类型: 0:定期巡查 5.日常巡查"），解析成 {code: name}。

解析不出来就返回空 dict —— 调用方原样输出代码，而不是硬翻一个可能错的名称。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .db import db_manager
from ..config import settings

_CACHE_TTL_SECONDS = settings.dictionary_ttl_seconds
_cache: dict[str, tuple[float, dict[str, str]]] = {}

_PAIR = re.compile(r"(?<!\d)(\d{1,3})\s*[:：.、,]?\s*([\u4e00-\u9fa5A-Za-z]{2,12})")


def code_labels(kind: str, table: str, column: str) -> dict[str, str]:
    """从列注释里解析 {code: 中文名}。"""
    key = f"{kind}:{table}.{column}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    labels: dict[str, str] = {}
    try:
        rows = db_manager.query(
            kind,
            "SELECT COLUMN_COMMENT AS comment FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND column_name = '{column}' LIMIT 1",
        )["rows"]
        comment = str(rows[0]["comment"]) if rows else ""
    except Exception:
        comment = ""
    if comment:
        for match in _PAIR.finditer(comment):
            code, name = match.group(1), match.group(2).strip()
            if not name or name.isdigit():
                continue
            labels.setdefault(code, name)
    _cache[key] = (time.time(), labels)
    return labels


def case_expression(column: str, labels: dict[str, str]) -> str:
    """把 {code: name} 变成 SQL CASE 表达式；没有映射就原样输出。"""
    if not labels:
        return column
    branches = " ".join(
        f"WHEN '{code}' THEN '{name}'" for code, name in labels.items()
    )
    return f"CASE {column} {branches} ELSE {column} END"


def problem_category_labels() -> dict[str, str]:
    return code_labels("mysql", "ai_problem_base", "disease_categories_type")


def patrol_type_labels() -> dict[str, str]:
    return code_labels("mysql", "ai_maintain_patrol", "patrol_type")


def patrol_status_labels() -> dict[str, str]:
    return code_labels("mysql", "ai_maintain_patrol", "patrol_status")


def surface_type_labels() -> dict[str, str]:
    return code_labels("mysql", "basic_facilities_sections", "surface_type")


def tech_grade_labels() -> dict[str, str]:
    return code_labels("mysql", "basic_facilities_sections", "tech_grade")
