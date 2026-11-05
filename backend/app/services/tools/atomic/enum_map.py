from __future__ import annotations

from typing import Any

from ..registry import tool


def _atomic(name: str, value: Any) -> dict[str, Any]:
    return {"ok": True, "tool": name, "value": value, "error": None}


@tool(
    name="resolve_damage_type",
    layer="atomic",
    domain="enum",
    description="把病害中文口语按现有字典缓存解析成标准类型和 problem_code；解析不到返回空列表。",
    input_schema={
        "type": "object",
        "properties": {"damage_text": {"type": "string"}},
        "required": ["damage_text"],
    },
    wraps="route_resolver._resolve_damage_type:115",
)
def resolve_damage_type(damage_text: str) -> dict[str, Any]:
    from ... import route_resolver

    try:
        value = route_resolver._resolve_damage_type(damage_text)
    except route_resolver.QueryPlanError:
        value = []
    return _atomic("resolve_damage_type", value)


@tool(
    name="code_map_from_comment",
    layer="atomic",
    domain="enum",
    description="从数据库列注释中提取 code→中文映射；少于两个编码对时返回空对象。",
    input_schema={
        "type": "object",
        "properties": {"comment": {"type": "string"}},
        "required": ["comment"],
    },
    wraps="schema_catalog._code_map_from_comment:282",
)
def code_map_from_comment(comment: str) -> dict[str, Any]:
    from ... import schema_catalog

    return _atomic("code_map_from_comment", schema_catalog._code_map_from_comment(comment))


@tool(
    name="field_code_map",
    layer="atomic",
    domain="enum",
    description="读取语义字段对应的 code→中文映射；该字段不是枚举时返回空对象。",
    input_schema={
        "type": "object",
        "properties": {"field": {"type": "string"}},
        "required": ["field"],
    },
    wraps="nl2sql._field_code_map:897",
)
def field_code_map(field: str) -> dict[str, Any]:
    from ... import nl2sql

    return _atomic("field_code_map", nl2sql._field_code_map(field))


@tool(
    name="normalize_damage_code_sql",
    layer="atomic",
    domain="enum",
    description="把病害类型旧关联 damage_type=problem_code 修正为 problem_base_id=id；修不了时拒绝执行。",
    input_schema={
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
    },
    wraps="sql_engine.normalize_damage_code_joins:112",
)
def normalize_damage_code_sql(sql: str) -> dict[str, Any]:
    from ... import sql_engine

    return _atomic("normalize_damage_code_sql", sql_engine.normalize_damage_code_joins(sql))
