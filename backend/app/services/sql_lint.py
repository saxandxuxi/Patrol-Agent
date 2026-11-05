"""SQL 结构体检：未知列、字段建议、字典 JOIN 风险。

从 `nl2sql.py` 拆出（docs/代码走查与目录结构重构方案.md 第三节）：
`nl2sql.py` 里按原名 re-export 结构检查和风险提示函数。

与 error_memory_hint / fix_mdl_sql（错误记忆 + 反思改 SQL）无关，那几支仍留在 nl2sql。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .db import db_manager
from .field_catalog import _catalog
from .logging_service import log_error


def unknown_columns(sql: str, kind: str = "mysql") -> list[str]:
    """SQL 里引用了**真实表里不存在**的列（物理 SQL 专用，不含 CTE 自造列）。

    见 docs/抽象题失败原因分析与整改方向.md 根因2：实测提示词里漏了语义层字段名
    `r.route_length_km`，物理表没这列 → 执行报 Unknown column → 整题放弃。
    这里在执行前先拦一道，把"用了不存在的列"当成反馈重新生成，而不是撞数据库报错。
    """
    catalog = _catalog(kind)
    known: set[str] = set()
    for table in catalog.get("tables") or []:
        for column in table.get("columns") or []:
            known.add(str(column.get("name") or "").lower())
    if not known:
        return []
    lower = sql.lower()
    # CTE / 子查询里 AS 出来的列名不算错
    derived = {name.lower() for name in re.findall(r"\bas\s+([a-z_][a-z0-9_]*)", lower)}
    derived |= {name.lower() for name in re.findall(r"\bwith\s+([a-z_][a-z0-9_]*)\s+as", lower)}
    unknown: list[str] = []
    for qualified in re.findall(r"\b[a-z_][a-z0-9_]*\.([a-z_][a-z0-9_]*)\b", lower):
        if qualified in known or qualified in derived or qualified in unknown:
            continue
        unknown.append(qualified)
    return unknown


def field_suggestions(names: list[str], kind: str = "mysql") -> list[str]:
    """不存在的列名 → 表结构里真实存在的列（按列名/列注释反查）。

    用途：把"用户/模型自己的叫法"翻译回库里的叫法，回喂给写 SQL 的模型重写，
    而不是让它照着同一个错名字再编一遍。
    """
    from . import wren_engine

    key = (kind, tuple(names))
    cached = _FIELD_SUGGEST_CACHE.get(key)
    if cached is not None:
        return list(cached)
    try:
        hints = wren_engine.suggest_fields(kind, names)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "field_suggest_failed", exc)
        hints = []
    if len(_FIELD_SUGGEST_CACHE) >= 500:
        _FIELD_SUGGEST_CACHE.clear()
    _FIELD_SUGGEST_CACHE[key] = tuple(hints)
    return hints


_FIELD_SUGGEST_CACHE: dict[tuple[str, tuple[str, ...]], tuple[str, ...]] = {}


_DICT_DUP_CACHE: dict[str, tuple[float, bool]] = {}


def _clean_sql_ident(value: str) -> str:
    return str(value or "").strip().strip("`\"[]").split(".")[-1].lower()


def _join_column_is_unique(table: str, column: str) -> bool:
    """右表关联列是否为主键/唯一键（当前字段目录至少能识别主键）。"""
    if not table or not column:
        return False
    try:
        from . import schema_catalog

        entry = schema_catalog.table_entry("mysql", _clean_sql_ident(table)) or {}
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "join_key_schema_failed", exc, table=table, column=column)
        return False
    for item in entry.get("columns") or []:
        if str(item.get("name") or "").lower() != str(column).lower():
            continue
        return "primary" in (item.get("roles") or []) or str(item.get("key") or "").upper() in {
            "PRI",
            "UNI",
        }
    return False


def _join_safe_on_unique_key(table: str, alias: str, condition: str) -> bool:
    """JOIN 条件是否包含“右表唯一键 = 任一左表列”。"""
    joined = {_clean_sql_ident(table), _clean_sql_ident(alias)}
    for left, right in re.findall(
        r"\b([a-z_][\w$]*(?:\.[a-z_][\w$]*)?)\s*=\s*([a-z_][\w$]*(?:\.[a-z_][\w$]*)?)\b",
        condition,
        re.I,
    ):
        left_alias = left.split(".", 1)[0].lower() if "." in left else ""
        right_alias = right.split(".", 1)[0].lower() if "." in right else ""
        if left_alias in joined and _join_column_is_unique(table, _clean_sql_ident(left)):
            return True
        if right_alias in joined and _join_column_is_unique(table, _clean_sql_ident(right)):
            return True
    return False


def _tenant_copied_dictionary(table: str) -> bool:
    """这张表是不是"按租户复制一份"的字典表（同 code 多行 → JOIN 会一对多翻倍）。

    判据（结构性，不看业务词）：表名以 `_base` 结尾或带 code 列，且有 tenant_id 列，
    并且（缓存 5 分钟）去重后行数少于总行数。
    """
    name = str(table or "").lower()
    if not name:
        return False
    cached = _DICT_DUP_CACHE.get(name)
    now = time.time()
    if cached and now - cached[0] < 300.0:
        return cached[1]
    try:
        from . import schema_catalog

        columns = {
            str(col.get("name")).lower()
            for col in (schema_catalog.table_entry("mysql", name) or {}).get("columns") or []
        }
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "tenant_dictionary_columns_failed", exc, table=name)
        columns = set()
    risky = False
    if "tenant_id" in columns:
        code_column = "problem_code" if "problem_code" in columns else ("code" if "code" in columns else "")
        if code_column:
            try:
                row = db_manager.query(
                    "mysql",
                    f"SELECT COUNT(*) AS total, COUNT(DISTINCT `{code_column}`) AS distinct_codes "
                    f"FROM `{name}` WHERE del_flag = 0"
                    if "del_flag" in columns
                    else f"SELECT COUNT(*) AS total, COUNT(DISTINCT `{code_column}`) AS distinct_codes FROM `{name}`",
                )["rows"][0]
                risky = int(row.get("total") or 0) > int(row.get("distinct_codes") or 0)
            except Exception as exc:  # noqa: BLE001
                # 不能静默：这里出错就意味着"翻倍护栏"失效（一开始就是 NameError 被吞掉）
                log_error("nl2sql", "tenant_dictionary_check_failed", exc, table=name)
                risky = False
    _DICT_DUP_CACHE[name] = (now, risky)
    return risky


def dictionary_join_issue(sql: str) -> str:
    """SQL 有没有“按非唯一 code 关联多租户表”的风险。

    见 docs/多租户字典去重与租户限定整改方案.md §3.1/§3.5：字典表随租户复制一份，
    `p.damage_type = b.problem_code` 这种 JOIN 会把行数放大（实测 1702 → 1872）。
    按主键 `id` 关联（如 disease.routes_id = route.id）天然是一对一，不应误报。
    本函数只返回风险说明；调用方不得把它当成阻断执行的错误。
    """
    text = str(sql or "")
    lower = text.lower()
    if "join" not in lower:
        return ""
    join_on = re.compile(
        r"\bjoin\s+([`\"\[]?[A-Za-z_][\w$.`\"\]]*)\s+"
        r"(?:(?:as\s+)?(?!on\b)([A-Za-z_][\w]*)\s+)?on\s+"
        r"(.+?)(?=\b(?:left|right|inner|outer|cross|straight_join|join|where|"
        r"group\s+by|order\s+by|having|limit|union)\b|$)",
        re.I | re.S,
    )
    for match in join_on.finditer(text):
        table = str(match.group(1))
        alias = str(match.group(2) or table)
        condition = str(match.group(3) or "")
        if _join_safe_on_unique_key(table, alias, condition):
            continue
        if not _tenant_copied_dictionary(table):
            continue
        if f"{alias}.tenant_id".lower() in lower or f"{table}.tenant_id".lower() in lower:
            continue
        return (
            f"JOIN {table} 按非唯一编码关联且没带同租户条件，可能一对多放大结果。"
            f"建议补 `{alias}.tenant_id` 与主表对齐，或先按关联编码去重。"
        )
    return ""


