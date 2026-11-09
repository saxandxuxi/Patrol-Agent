from __future__ import annotations

from typing import Any


_PILE_FIELDS = frozenset({"pile_number", "pile_distance"})


def partial_coverage_scope_notice(verdicts: Any) -> str:
    """给部分覆盖的数据结论加上范围降级，而不是只在末尾附台账时间。"""

    if "partial" not in {
        str(item or "").strip().lower() for item in verdicts
    }:
        return ""
    return (
        "覆盖范围不完整：以下结果只代表实际有记录的部分；"
        "未覆盖区间的数据可能改变总量、趋势或排名，"
        "这里只能给出已覆盖部分的观察结果"
    )


def explicit_range_note(spec: Any) -> str:
    """说明结构化闭区间过滤的边界口径。"""
    for item in getattr(spec, "filters", ()) or ():
        field = str(getattr(item, "field", "")).rsplit(".", 1)[-1]
        if field not in _PILE_FIELDS or str(getattr(item, "op", "")) != "between":
            continue
        values = list(getattr(item, "storage_value", ()) or ())
        if not isinstance(getattr(item, "storage_value", None), (list, tuple)):
            continue
        if len(values) != 2:
            continue
        labels = _display_bounds(item, values)
        if not labels:
            continue
        return (
            f"边界口径：桩号区间按闭区间处理，{labels[0]} 和 "
            f"{labels[1]} 两端均包含。"
        )
    return ""


def _display_bounds(item: Any, values: list[Any]) -> tuple[str, str] | None:
    display = str(getattr(item, "display_value", "") or "").strip()
    for separator in ("，", "、", ","):
        if separator not in display:
            continue
        parts = [part.strip() for part in display.split(separator)]
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
    try:
        from ...services.calibrate import fmt_pile

        return fmt_pile(values[0]), fmt_pile(values[1])
    except (TypeError, ValueError):
        return None
