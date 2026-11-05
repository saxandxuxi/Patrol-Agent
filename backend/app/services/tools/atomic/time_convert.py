from __future__ import annotations

from datetime import datetime
from typing import Any

from ..registry import tool


def _atomic(name: str, value: Any) -> dict[str, Any]:
    return {"ok": True, "tool": name, "value": value, "error": None}


def _range_value(value: tuple[datetime, datetime] | None) -> list[str] | None:
    if not value:
        return None
    return [value[0].isoformat(sep=" "), value[1].isoformat(sep=" ")]


def _now(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@tool(
    name="iso_week_range",
    layer="atomic",
    domain="time",
    description="把“某年第几周”换算成 ISO 周一到周日的左闭右开区间。日常查询无需调用，只有链式回填或区间纠错时使用。",
    input_schema={
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "now": {"type": ["string", "null"], "description": "ISO 参照时间，省略则用当前时间"},
        },
        "required": ["message"],
    },
    wraps="parsers._iso_week_range:359",
)
def iso_week_range(message: str, now: str | None = None) -> dict[str, Any]:
    from ... import parsers

    value = parsers._iso_week_range(message, _now(now) or datetime.now())
    return _atomic("iso_week_range", _range_value(value))


@tool(
    name="quarter_range",
    layer="atomic",
    domain="time",
    description="把“某年第 N 季度”换算成正好三个月的左闭右开区间。",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}, "now": {"type": ["string", "null"]}},
        "required": ["message"],
    },
    wraps="parsers._quarter_range:339",
)
def quarter_range(message: str, now: str | None = None) -> dict[str, Any]:
    from ... import parsers

    value = parsers._quarter_range(message, _now(now) or datetime.now())
    return _atomic("quarter_range", _range_value(value))


@tool(
    name="month_range",
    layer="atomic",
    domain="time",
    description="把“某年某月/某月”换算成整月左闭右开区间。",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}, "now": {"type": ["string", "null"]}},
        "required": ["message"],
    },
    wraps="parsers.parse_month_range:378",
)
def month_range(message: str, now: str | None = None) -> dict[str, Any]:
    from ... import parsers

    return _atomic("month_range", _range_value(parsers.parse_month_range(message, _now(now))))


@tool(
    name="parse_time_range",
    layer="atomic",
    domain="time",
    description="按现有规则从自然语言中解析时间区间；语义层已给出结构时优先用 time_range_from_spec。",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}, "now": {"type": ["string", "null"]}},
        "required": ["message"],
    },
    wraps="parsers.parse_time_range:396",
)
def parse_time_range(message: str, now: str | None = None) -> dict[str, Any]:
    from ... import parsers

    return _atomic(
        "parse_time_range",
        _range_value(parsers.parse_time_range(message, _now(now))),
    )


@tool(
    name="time_range_from_spec",
    layer="atomic",
    domain="time",
    description="把语义层给出的结构化 time spec 做纯算术换算，结果可能是 null。",
    input_schema={
        "type": "object",
        "properties": {
            "spec": {"type": "object"},
            "now": {"type": ["string", "null"]},
        },
        "required": ["spec"],
    },
    wraps="parsers.time_range_from_spec:180",
)
def time_range_from_spec(spec: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    from ... import parsers

    value = parsers.time_range_from_spec(spec, _now(now))
    return _atomic("time_range_from_spec", _range_value(value))
