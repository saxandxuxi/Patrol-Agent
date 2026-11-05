"""宏观元数据查询（库/表/字段本身的结构信息）。

见 docs/宏观元数据查询_语义理解方案.md：
  层 1 意图分类（intent_service，LLM）判出 metadata；
  层 2 本模块 parse_metadata_request 用 LLM 解析诉求（输出 JSON）；
  层 3 execute_metadata 按 JSON 确定性拼 information_schema / pg_catalog，不调 LLM。

为什么不靠关键词触发：问法千变万化（"库里都有啥表""哪些表还没用上""哪张表记录最大"），
词表必然漏。判断权交给语义理解；SQL 由代码按 JSON 拼，模型不写 SQL，不会 hallucinate。
"""

from __future__ import annotations

from typing import Any

from ..config import settings
from . import prompts
from .db import db_manager
from .llm import LLMError, chat_json, role_model
from .logging_service import log_error, log_event

_KINDS = ("mysql", "postgresql")

# 表级概览：一张表一行（表名 / 表说明 / 行数估算）。
# 行数用 information_schema.TABLES.TABLE_ROWS（InnoDB 估算值，可能偏差较大），
# 需要精确时再对少数几张表跑 COUNT(*)（见 METADATA_REFINE_TOP_N）。
_TABLE_SQL = {
    "mysql": (
        "SELECT TABLE_NAME AS table_name, TABLE_COMMENT AS table_comment, "
        "TABLE_ROWS AS row_estimate "
        "FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_NAME"
    ),
    "postgresql": (
        "SELECT c.relname AS table_name, "
        "COALESCE(obj_description(c.oid, 'pg_class'), '') AS table_comment, "
        "COALESCE(NULLIF(s.n_live_tup, 0), GREATEST(c.reltuples, 0))::bigint AS row_estimate "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
        "ORDER BY c.relname"
    ),
}

_COLUMN_SQL = {
    "mysql": (
        "SELECT COLUMN_NAME AS column_name, COLUMN_TYPE AS column_type, "
        "IS_NULLABLE AS is_nullable, COLUMN_KEY AS column_key, "
        "COLUMN_COMMENT AS column_comment "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table}' "
        "ORDER BY ORDINAL_POSITION"
    ),
    "postgresql": (
        "SELECT column_name, data_type AS column_type, is_nullable, "
        "'' AS column_key, "
        "COALESCE(col_description(('public.' || table_name)::regclass, ordinal_position), '') "
        "AS column_comment "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = '{table}' "
        "ORDER BY ordinal_position"
    ),
}


def _sql_literal(value: str) -> str:
    return str(value).replace("'", "''")


def _quoted(kind: str, table: str) -> str:
    if kind == "mysql":
        return "`" + str(table).replace("`", "``") + "`"
    return '"' + str(table).replace('"', '""') + '"'


def _kind_label(kind: str) -> str:
    return "MySQL" if kind == "mysql" else "PostgreSQL"


def table_overview(kind: str) -> list[dict[str, Any]]:
    """库里所有基表：表名 / 说明 / 行数估算。读库失败时抛异常（调用方决定怎么降级）。"""
    result = db_manager.query(kind, _TABLE_SQL[kind])
    tables: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        name = str(row.get("table_name") or "").strip()
        if not name:
            continue
        raw_rows = row.get("row_estimate")
        try:
            rows = int(float(raw_rows))
        except (TypeError, ValueError):
            rows = None
        tables.append(
            {
                "table": name,
                "comment": str(row.get("table_comment") or "").strip(),
                "rows": rows,
            }
        )
    return tables


def table_columns(kind: str, table: str) -> dict[str, Any]:
    sql = _COLUMN_SQL[kind].format(table=_sql_literal(table))
    result = db_manager.query(kind, sql)
    columns: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        columns.append(
            {
                "column": str(row.get("column_name") or ""),
                "type": str(row.get("column_type") or ""),
                "nullable": str(row.get("is_nullable") or ""),
                "key": str(row.get("column_key") or ""),
                "comment": str(row.get("column_comment") or "").strip(),
            }
        )
    return {"columns": columns, "sql": sql}


def _count_rows(kind: str, table: str) -> int | None:
    try:
        rows = db_manager.query(kind, f"SELECT COUNT(*) AS c FROM {_quoted(kind, table)}")["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("metadata", "count_failed", exc, kind=kind, table=table)
        return None
    if not rows:
        return None
    try:
        return int(float(rows[0].get("c") or 0))
    except (TypeError, ValueError):
        return None


def _refine_rows(kind: str, tables: list[dict[str, Any]], limit: int) -> int:
    """对前 limit 张表跑精确 COUNT(*)（估算值偏差大时排序会错）。返回精确统计了几张。"""
    count = 0
    for item in tables[: max(int(limit), 0)]:
        exact = _count_rows(kind, str(item["table"]))
        if exact is not None:
            item["rows"] = exact
            item["exact"] = True
            count += 1
    return count


def _catalog_tables(kind: str) -> list[dict[str, Any]]:
    """白名单内的表名 + 中文名（从字段目录拿，带缓存）——给解析提示词当参考。"""
    try:
        from . import schema_catalog

        return [
            {"table": str(item.get("table")), "label": str(item.get("label") or "")}
            for item in schema_catalog.field_catalog(kind).get("tables") or []
        ]
    except Exception as exc:  # noqa: BLE001
        log_error("metadata", "catalog_hint_failed", exc, kind=kind)
        return []


def parse_metadata_request(message: str) -> dict[str, Any] | None:
    """层 2：LLM 解析"用户在问库/表/字段的什么"。判不出是元数据查询时返回 None。"""
    lines: list[str] = []
    for kind in _KINDS:
        items = _catalog_tables(kind)
        if not items:
            continue
        lines.append(f"[{_kind_label(kind)}]")
        lines.extend(
            f"- {item['table']}（{item['label']}）" if item["label"] else f"- {item['table']}"
            for item in items
        )
    tables = "\n".join(lines) or "（未取到表清单，只能按用户点名的表名判断）"
    try:
        data = chat_json(
            prompts.messages(
                "metadata",
                databases="、".join(_kind_label(item) for item in _KINDS),
                tables=tables,
                message=message,
            ),
            model=role_model("semantic"),
        )
    except (LLMError, Exception) as exc:  # noqa: BLE001
        log_error("metadata", "parse_failed", exc, question=message[:80])
        return None
    return _normalize_parsed(data)


def _normalize_parsed(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    obj = str(data.get("object") or "").strip().lower()
    if obj not in {"table", "column", "database"}:
        return None
    action = str(data.get("action") or "list").strip().lower()
    if action not in {"list", "count", "rank", "filter_empty", "filter_nonempty"}:
        action = "list"
    order = "asc" if str(data.get("order") or "").strip().lower() == "asc" else "desc"
    kinds: list[str] = []
    for item in data.get("databases") or []:
        name = str(item or "").strip().lower()
        if name in {"mysql", "postgres", "postgresql", "pg"}:
            resolved = "mysql" if name == "mysql" else "postgresql"
            if resolved not in kinds:
                kinds.append(resolved)
    target = str(data.get("target_table") or "").strip() or None
    return {
        "object": obj,
        "action": action,
        "order": order,
        "databases": kinds or ["mysql"],
        "target_table": target,
    }


def resolve_target_table(
    kind: str,
    target: str | None,
    message: str,
    live_tables: list[dict[str, Any]] | None = None,
) -> str | None:
    """把"表名/中文名"解析成真实表名：目录精确匹配 → 库里的真实表 → 模糊匹配。"""
    wanted = str(target or "").strip()
    names = [str(item.get("table")) for item in (live_tables or [])]
    if wanted:
        for name in names:
            if name.lower() == wanted.lower():
                return name
        try:
            from . import schema_catalog

            resolved = schema_catalog.resolve_table(kind, wanted)
        except Exception as exc:  # noqa: BLE001
            log_error("metadata", "resolve_table_failed", exc, kind=kind)
            resolved = None
        if resolved and (not names or resolved in names):
            return resolved
        lowered = wanted.lower()
        fuzzy = [name for name in names if lowered in name.lower()]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if not names:
            return None
    # 提示词没给表名时，拿问题原文去撞表名/中文名（仍然只在库里真实存在的表里挑）
    text = str(message or "").lower()
    for name in names:
        if name.lower() in text:
            return name
    try:
        from . import schema_catalog

        for item in schema_catalog.field_catalog(kind).get("tables") or []:
            label = str(item.get("label") or "").lower()
            if label and label in text and (not names or str(item.get("table")) in names):
                return str(item.get("table"))
    except Exception as exc:  # noqa: BLE001
        log_error("metadata", "resolve_label_failed", exc, kind=kind)
    return None


def _columns_for(
    kind: str, target: str | None, message: str
) -> tuple[str | None, dict[str, Any] | None]:
    """定位"要查字段的那张表"：先按给的名字直接查列，查不到再拿库里真实表清单解析。"""
    if target:
        try:
            found = table_columns(kind, target)
        except Exception as exc:  # noqa: BLE001
            log_error("metadata", "columns_direct_failed", exc, kind=kind, table=target)
            found = None
        if found and found.get("columns"):
            return target, found
    try:
        live = table_overview(kind)
    except Exception as exc:  # noqa: BLE001
        log_error("metadata", "overview_for_resolve_failed", exc, kind=kind)
        live = []
    name = resolve_target_table(kind, target, message, live)
    if not name:
        return None, None
    try:
        found = table_columns(kind, name)
    except Exception as exc:  # noqa: BLE001
        log_error("metadata", "columns_failed", exc, kind=kind, table=name)
        return None, None
    if not found.get("columns"):
        return None, None
    return name, found


def _table_line(index: int, item: dict[str, Any]) -> str:
    name = str(item.get("table"))
    comment = str(item.get("comment") or "")
    rows = item.get("rows")
    if rows is None:
        size = "行数未知"
    else:
        size = f"{rows} 行" if item.get("exact") else f"约 {rows} 行"
    suffix = f"（{comment}）" if comment else ""
    return f"{index}. {name}{suffix} — {size}"


def _column_line(index: int, item: dict[str, Any]) -> str:
    parts = [str(item.get("type") or "")]
    if str(item.get("nullable")).upper() == "NO":
        parts.append("非空")
    if str(item.get("key") or "").upper() in {"PRI", "PRIMARY"}:
        parts.append("主键")
    comment = str(item.get("comment") or "")
    if comment:
        parts.append(comment)
    detail = "，".join(part for part in parts if part)
    return f"{index}. {item.get('column')} — {detail}" if detail else f"{index}. {item.get('column')}"


def _table_rows_payload(kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "库": _kind_label(kind),
            "表名": str(item.get("table")),
            "说明": str(item.get("comment") or ""),
            "行数": item.get("rows"),
        }
        for item in items
    ]


def execute_metadata(parsed: dict[str, Any], message: str = "") -> dict[str, Any]:
    """层 3：按解析出的 JSON **确定性**拼 SQL 并执行（不调 LLM）。"""
    obj = str(parsed.get("object"))
    action = str(parsed.get("action"))
    order = str(parsed.get("order") or "desc")
    kinds = [kind for kind in parsed.get("databases") or [] if kind in _KINDS] or ["mysql"]
    limit = max(int(getattr(settings, "metadata_list_limit", 200) or 200), 1)
    refine = max(int(getattr(settings, "metadata_refine_top_n", 10) or 0), 0)

    sqls: list[str] = []
    rows: list[dict[str, Any]] = []
    columns: list[str] = []
    blocks: list[str] = []

    if obj == "column":
        columns = ["库", "表名", "字段", "类型", "可空", "主键", "说明"]
        for kind in kinds:
            name, found = _columns_for(kind, parsed.get("target_table"), message)
            if not name or not found:
                blocks.append(
                    f"{_kind_label(kind)} 库里没定位到要查的表（用表名或中文名告诉我具体是哪张表）。"
                )
                continue
            sqls.append(str(found.get("sql") or ""))
            items = list(found.get("columns") or [])
            for item in items:
                rows.append(
                    {
                        "库": _kind_label(kind),
                        "表名": name,
                        "字段": item.get("column"),
                        "类型": item.get("type"),
                        "可空": item.get("nullable"),
                        "主键": item.get("key"),
                        "说明": item.get("comment"),
                    }
                )
            body = "\n".join(
                _column_line(index, item) for index, item in enumerate(items[:limit], start=1)
            )
            head = f"{_kind_label(kind)} 库的 {name} 表共有 {len(items)} 个字段："
            blocks.append(head + ("\n" + body if body else "（没有字段）"))
            if len(items) > limit:
                blocks.append(f"（只列出前 {limit} 个字段，还有 {len(items) - limit} 个未列出）")
        return {"reply": "\n".join(blocks), "sql": sqls, "columns": columns, "rows": rows}

    # object = table / database：都是"表清单 + 行数"，只是口径不同
    for kind in kinds:
        try:
            tables = table_overview(kind)
        except Exception as exc:  # noqa: BLE001
            log_error("metadata", "overview_failed", exc, kind=kind)
            blocks.append(f"{_kind_label(kind)} 库这次读不通，没取到表清单（{str(exc)[:60]}）。")
            continue
        sqls.append(_TABLE_SQL[kind])
        for item in tables:
            item["comment"] = str(item.get("comment") or "")

        def _size(item: dict[str, Any]) -> int:
            value = item.get("rows")
            return int(value) if isinstance(value, (int, float)) else 0

        # 行数取不到的表（少数引擎/权限下 TABLE_ROWS 是 NULL）：不参与"空表/有数据"判断，
        # 免得把"行数未知"当成"空表"如实说明成错的
        unknown = [item for item in tables if not isinstance(item.get("rows"), (int, float))]
        total = len(tables)
        if action == "rank":
            tables.sort(key=lambda item: -_size(item))
            if refine:
                _refine_rows(kind, tables, min(refine, total))
            tables.sort(key=lambda item: (-_size(item) if order == "desc" else _size(item)))
            kept = tables[:limit]
            rows.extend(_table_rows_payload(kind, kept))
            exact = sum(1 for item in kept if item.get("exact"))
            note = "（已用 COUNT(*) 精确统计）" if exact else "（行数为统计信息估算值）"
            blocks.append(f"{_kind_label(kind)} 库记录数排名{note}：")
            blocks.append(
                "\n".join(
                    _table_line(index, item) for index, item in enumerate(kept, start=1)
                )
                or "（库里没有表）"
            )
        elif action == "filter_empty":
            empties = [
                item
                for item in tables
                if isinstance(item.get("rows"), (int, float)) and _size(item) == 0
            ]
            rows.extend(_table_rows_payload(kind, empties[:limit]))
            blocks.append(
                f"{_kind_label(kind)} 库共 {total} 张表，其中 {len(empties)} 张是空表"
                "（表里还没有数据）："
            )
            blocks.append(
                "\n".join(
                    _table_line(index, item) for index, item in enumerate(empties[:limit], start=1)
                )
                or "（没有空表）"
            )
            if len(empties) > limit:
                blocks.append(f"（还有 {len(empties) - limit} 张未列出）")
            if unknown:
                blocks.append(f"（另有 {len(unknown)} 张表的行数取不到，没纳入空表判断）")
        elif action == "filter_nonempty":
            filled = [
                item
                for item in tables
                if isinstance(item.get("rows"), (int, float)) and _size(item) > 0
            ]
            filled.sort(key=lambda item: -_size(item))
            rows.extend(_table_rows_payload(kind, filled[:limit]))
            blocks.append(
                f"{_kind_label(kind)} 库共 {total} 张表，其中 {len(filled)} 张有数据（行数为估算值）："
            )
            blocks.append(
                "\n".join(
                    _table_line(index, item) for index, item in enumerate(filled[:limit], start=1)
                )
                or "（没有有数据的表）"
            )
            if len(filled) > limit:
                blocks.append(f"（还有 {len(filled) - limit} 张未列出）")
            if unknown:
                blocks.append(f"（另有 {len(unknown)} 张表的行数取不到，没纳入判断）")
        elif action == "count":
            rows.extend(_table_rows_payload(kind, tables))
            blocks.append(f"{_kind_label(kind)} 库共有 {total} 张表。")
        else:  # list
            rows.extend(_table_rows_payload(kind, tables[:limit]))
            blocks.append(f"{_kind_label(kind)} 库共有 {total} 张表（行数为统计信息估算值）：")
            blocks.append(
                "\n".join(
                    _table_line(index, item) for index, item in enumerate(tables[:limit], start=1)
                )
                or "（库里没有表）"
            )
            if total > limit:
                blocks.append(f"（只列出前 {limit} 张，还有 {total - limit} 张未列出）")

    columns = columns or ["库", "表名", "说明", "行数"]
    return {"reply": "\n".join(blocks), "sql": sqls, "columns": columns, "rows": rows}


def metadata_payload(message: str) -> dict[str, Any] | None:
    """对外入口：语义解析（LLM）+ 确定性执行。判不出是元数据查询时返回 None。"""
    import time as _time

    started = _time.perf_counter()
    parsed = parse_metadata_request(message)
    if not parsed:
        return None
    try:
        result = execute_metadata(parsed, message)
    except Exception as exc:  # noqa: BLE001
        log_error("metadata", "execute_failed", exc, question=message[:80])
        return None
    reply = str(result.get("reply") or "").strip()
    if not reply:
        return None
    log_event(
        "metadata",
        "INFO",
        "executed",
        ms=round((_time.perf_counter() - started) * 1000),
        object=parsed.get("object"),
        action=parsed.get("action"),
        databases=",".join(parsed.get("databases") or []),
        rows=len(result.get("rows") or []),
    )
    return {
        "reply": reply,
        "source": "database",
        "engine": "metadata",
        "answer_format": "text",
        "sql": "\n".join(item for item in result.get("sql") or [] if item) or None,
        "columns": list(result.get("columns") or []),
        "rows": list(result.get("rows") or []),
        "chart_data": None,
        # 元数据查询是确定性拼出来的，不需要"答所问"校验/反思/后续建议
        "direct": True,
    }
