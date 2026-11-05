from __future__ import annotations

from typing import Any

from ..registry import tool


def _atomic(name: str, value: Any) -> dict[str, Any]:
    return {"ok": True, "tool": name, "value": value, "error": None}


@tool(
    name="fill_placeholders",
    layer="atomic",
    domain="chain",
    description="把 {{key}} 占位符替换成前序步骤真实产出值，并返回缺失键。",
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "values": {"type": "object"},
        },
        "required": ["question", "values"],
    },
    wraps="agent._fill_placeholders:2460",
)
def fill_placeholders(question: str, values: dict[str, str]) -> dict[str, Any]:
    from ... import agent

    filled, missing = agent._fill_placeholders(question, values)
    return _atomic("fill_placeholders", {"question": filled, "missing": missing})


@tool(
    name="extract_produced",
    layer="atomic",
    domain="chain",
    description="按 produces.from_column 从前序结果行提取回填值；多行时拼成可读标签列表。",
    input_schema={
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": {"type": "object"}},
            "item": {"type": "object"},
        },
        "required": ["rows", "item"],
    },
    wraps="agent._extract_produced:2475",
)
def extract_produced(rows: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any]:
    from ... import agent

    return _atomic("extract_produced", agent._extract_produced(rows, item))
