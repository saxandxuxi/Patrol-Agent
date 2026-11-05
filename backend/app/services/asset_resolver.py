# -*- coding: utf-8 -*-
"""桥/隧/边坡的"名称 → ID"解析（docs/桥隧边坡资产名称ID解析方案.md）。

以前只有路线能解析名字（`route_resolver._resolve_route("西乡段") → 路线 id`），
问"XX桥的技术状况"只能落到 bridges 表、靠 LLM 自己猜桥名怎么写 → 查错或查空。
这里照 route_resolver 的做法补上：**只读**、带 TTL 缓存、精确优先、同名多座不硬猜。

注意：三张表现在都是空的（0 行），所以今天解析不出任何名字——代码先备好，
数据一到就能用；`--fixtures` 那套夹具可以拿来验这条链路（见 fixture_lab）。
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..config import settings
from . import text_match
from .db import db_manager
from .logging_service import log_error, log_event

# 主体/表名 → (表, 名称列, 后缀词)。后缀词用来从问句里切出"像资产名"的片段：
# "罗田桥/羊台山隧道/月亮湾边坡"这类用户写法最常见。
ASSET_TABLES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "bridge": ("basic_facilities_bridges", ("name", "code"), ("桥",)),
    "tunnel": ("basic_facilities_tunnels", ("name", "asset_code"), ("隧道",)),
    "slope": ("basic_facilities_slope", ("name", "code"), ("边坡",)),
    "device": (
        "ai_collection_equipment",
        ("device_name", "device_code"),
        ("设备",),
    ),
}

_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_TTL = float(settings.reference_data_ttl_seconds)


def table_of(subject: str) -> str:
    """主体名 → 物理表（bridge/tunnel/slope）。"""
    item = ASSET_TABLES.get(str(subject or "").lower())
    return item[0] if item else ""


def subject_of(table: str) -> str:
    """物理表 → 主体名（反查）。"""
    for subject, (name, _, _) in ASSET_TABLES.items():
        if name == table:
            return subject
    return ""


def rows_of(subject: str) -> list[dict[str, str]]:
    """该表的 id + 名称列，带 TTL 缓存；查不通时用旧缓存，没有旧缓存就返回空（下次再试）。"""
    item = ASSET_TABLES.get(str(subject or "").lower())
    if not item:
        return []
    table, name_columns, _ = item
    cached = _CACHE.get(subject)
    now = time.time()
    if cached and now - cached[0] < _TTL:
        return cached[1]
    columns = ", ".join(dict.fromkeys(("id",) + name_columns))
    try:
        rows = db_manager.query(
            "mysql", f"SELECT {columns} FROM {table} WHERE del_flag = 0 LIMIT 5000"
        )["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("asset_resolver", "asset_rows_failed", exc, table=table)
        return cached[1] if cached else []
    out = [
        {
            "id": str(row.get("id") or ""),
            "name": next(
                (str(row.get(col) or "") for col in name_columns if str(row.get(col) or "").strip()),
                "",
            ),
            "code": str(row.get("code") or row.get("asset_code") or ""),
            "device_code": str(row.get("device_code") or ""),
        }
        for row in rows
    ]
    _CACHE[subject] = (now, out)
    return out


def name_tokens(message: str, subject: str) -> list[str]:
    """问句里"像这个资产名"的片段：`罗田桥` 这种带后缀的词，长度 2~12。"""
    item = ASSET_TABLES.get(str(subject or "").lower())
    if not item:
        return []
    _, _, suffixes = item
    tokens: list[str] = []
    for suffix in suffixes:
        for match in re.finditer(rf"[\u4e00-\u9fffA-Za-z0-9]{{1,10}}{re.escape(suffix)}", message):
            token = match.group(0)
            # 去掉"这座/哪座/所有/的"这类前缀噪声，只留名字本体
            token = re.sub(
                r"^(这座|那座|哪座|所有|全部|每座|该|的|了|最长|最短|最大|最小|最高|最低"
                r"|最旧|最新|最早|最晚|最)+",
                "",
                token,
            )
            # 只剩后缀（"隧道/桥/边坡"）说明用户没点名具体资产（"最长的隧道是哪条"）：
            # 这类最高级问题该 ORDER BY 长度，不该被当成"名字候选"去反问
            if len(token) <= len(suffix) or len(token) > 12:
                continue
            if token not in tokens:
                tokens.append(token)
    return tokens


def resolve(kind: str, subject: str, message: str) -> dict[str, Any]:
    """把问题里的资产名解析成 id（精确 > 前缀/包含 > 无）。

    返回 `{"id":…, "name":…, "candidates":[…], "token":…}`；解析不出来给空 id。
    同名多座不硬猜：`candidates` 列出来（调用方把它写进口径/反问，或让 LLM 选）。
    """
    tokens = name_tokens(message, subject)
    if not tokens:
        return {"id": "", "name": "", "candidates": [], "token": ""}
    rows = rows_of(subject)
    if not rows:
        return {"id": "", "name": "", "candidates": [], "token": tokens[0]}
    for token in tokens:
        exact = [row for row in rows if row["name"] == token]
        if len(exact) == 1:
            return {"id": exact[0]["id"], "name": exact[0]["name"], "candidates": [], "token": token}
        if len(exact) > 1:
            return {"id": "", "name": "", "candidates": exact[:5], "token": token}
    for token in tokens:
        fuzzy = text_match.rank_rows(token, rows, ("name", "code"))
        best = text_match.unique_best(fuzzy)
        if best:
            log_event(
                "asset_resolver",
                "INFO",
                "asset_name_fuzzy",
                token=token,
                matched=best["name"],
                subject=subject,
            )
            return {"id": best["id"], "name": best["name"], "candidates": [], "token": token}
        if len(fuzzy) > 1:
            return {"id": "", "name": "", "candidates": fuzzy[:5], "token": token}
    return {"id": "", "name": "", "candidates": [], "token": tokens[0]}


def resolve_device(mention: str) -> dict[str, Any]:
    """设备短号 → 唯一 device_code；台账名称或编码后缀命中唯一才采用。"""
    text = str(mention or "").strip()
    if not text:
        return {"code": "", "name": "", "candidates": [], "token": ""}
    rows = rows_of("device")
    exact = [
        row
        for row in rows
        if text in {str(row.get("name") or ""), str(row.get("device_code") or "")}
    ]
    suffix = [
        row
        for row in rows
        if len(text) >= 3
        and (
            str(row.get("name") or "").lower().endswith(text.lower())
            or str(row.get("device_code") or "").lower().endswith(text.lower())
        )
    ]
    matched = exact or suffix
    if len(matched) == 1:
        row = matched[0]
        return {
            "code": str(row.get("device_code") or ""),
            "name": str(row.get("name") or ""),
            "candidates": [],
            "token": text,
        }
    return {
        "code": "",
        "name": "",
        "candidates": matched[:5],
        "token": text,
    }


def clear_cache() -> None:
    _CACHE.clear()
