from __future__ import annotations

from typing import Any

from ..registry import tool


def _atomic(name: str, value: Any) -> dict[str, Any]:
    return {"ok": True, "tool": name, "value": value, "error": None}


@tool(
    name="parse_pile_range",
    layer="atomic",
    domain="pile",
    description="把“K1+200~K1+800”换算成米区间；单点默认转成向后 999 米的查询区间。",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
    wraps="parsers.parse_pile_range:32",
)
def parse_pile_range(message: str) -> dict[str, Any]:
    from ... import parsers

    value = parsers.parse_pile_range(message)
    return _atomic("parse_pile_range", list(value) if value else None)


@tool(
    name="parse_pile_side",
    layer="atomic",
    domain="pile",
    description="把“K1+200 以西/以东”换算成别名、标签和 SQL 过滤片段。",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
    wraps="parsers.parse_pile_side:157",
)
def parse_pile_side(message: str) -> dict[str, Any]:
    from ... import parsers

    value = parsers.parse_pile_side(message)
    return _atomic("parse_pile_side", list(value) if value else None)


@tool(
    name="pile_bucket_meters",
    layer="atomic",
    domain="pile",
    description="从“每 100 米一段/每 1 公里一段”里读取分桶米数。",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
    wraps="nl2sql._pile_bucket_meters:685",
)
def pile_bucket_meters(message: str) -> dict[str, Any]:
    from ... import nl2sql

    return _atomic("pile_bucket_meters", nl2sql._pile_bucket_meters(message))


@tool(
    name="format_pile",
    layer="atomic",
    domain="pile",
    description="把桩号米数格式化成 K 形式；空值返回空字符串。",
    input_schema={
        "type": "object",
        "properties": {"distance": {"type": ["integer", "number", "null"]}},
        "required": ["distance"],
    },
    wraps="calibrate.fmt_pile:24",
)
def format_pile(distance: int | float | None) -> dict[str, Any]:
    from ... import calibrate

    return _atomic("format_pile", calibrate.fmt_pile(distance))
