from __future__ import annotations

import copy
from typing import Any

from ..observation import observation
from ..registry import tool


def _fact_range(facts: dict[str, Any]) -> dict[str, str]:
    return {
        "time_start": str(facts.get("data_min_time") or ""),
        "time_end": str(facts.get("data_max_time") or ""),
        "pile_start": str(facts.get("data_min_pile") or ""),
        "pile_end": str(facts.get("data_max_pile") or ""),
    }


@tool(
    name="check_coverage",
    layer="big",
    domain="coverage",
    description="用现有校准器核对计划中的时间、桩号和路线覆盖范围；只判断范围，不执行查询。",
    input_schema={
        "type": "object",
        "properties": {"plan": {"type": "object"}},
        "required": ["plan"],
    },
    wraps="calibrate.check:158",
)
def check_coverage(plan: dict[str, Any]) -> dict[str, Any]:
    from ... import calibrate

    current = copy.deepcopy(plan)
    current.setdefault("kind", "mysql")
    current.setdefault("subject", "disease")
    current.setdefault("slots", {})
    try:
        result = calibrate.check(current)
        route = (current.get("slots") or {}).get("route_match") or {}
        facts = (
            calibrate.route_facts(str(route["id"]))
            if route.get("id")
            else calibrate.global_data_range()
        )
    except Exception as exc:  # noqa: BLE001
        return observation(
            "check_coverage",
            ok=False,
            summary=f"覆盖范围核对失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="检查查询计划中的路线、时间和桩号字段。",
        )
    slots = current.get("slots") or {}
    asked = {
        "start": str(slots.get("time_start") or slots.get("pile_start") or ""),
        "end": str(slots.get("time_end") or slots.get("pile_end") or ""),
    }
    actual = _fact_range(facts)
    if not result:
        return observation(
            "check_coverage",
            ok=True,
            summary="查询范围在现有数据覆盖内。",
            data={
                "row_count": 0,
                "columns": [],
                "preview_rows": [],
                "produced": {},
                "truncated": False,
                "coverage": "full",
            },
            coverage={"asked_range": asked, "actual_range": actual, "empty": False},
        )
    return observation(
        "check_coverage",
        ok=True,
        summary=str(result.get("message") or ""),
        data={
            "row_count": 0,
            "columns": [],
            "preview_rows": [],
            "produced": {},
            "truncated": False,
            "coverage": result.get("coverage"),
            "kind": result.get("kind"),
        },
        coverage={
            "asked_range": asked,
            "actual_range": actual,
            "empty": result.get("coverage") == "none",
        },
        next_hint="范围外时先换相邻有效范围，或如实说明实际覆盖。",
    )
