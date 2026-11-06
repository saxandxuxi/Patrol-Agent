from __future__ import annotations

from typing import Any

from ..models import CalculationRequest, CalculationResult
from .ranking import select_boundary_row


def render_column_calculation(
    result: CalculationResult,
    request: CalculationRequest,
    task: dict[str, Any],
    output_field: str,
) -> str:
    lower_is_better = "lower_is_better" in {
        str(item) for item in task.get("required_assertions") or ()
    }
    selected = select_boundary_row(
        result.rows,
        output_field,
        lower_is_better=lower_is_better,
    )
    if selected is None:
        return "列计算没有可用结果。"
    label_field = next(
        (
            key
            for key, value in selected.items()
            if key != output_field
            and value not in (None, "")
            and not isinstance(value, (int, float))
        ),
        "",
    )
    label = (
        f"{label_field}={selected.get(label_field)}，"
        if label_field
        else ""
    )
    unit = (
        request.output.unit
        if request.output and request.output.unit
        else ""
    )
    excluded = list(result.evidence.get("excluded_row_ids") or [])
    exclusion_note = (
        f"；已排除 {len(excluded)} 个零长度行："
        + "、".join(excluded[:5])
        if excluded
        else ""
    )
    return (
        f"已计算 {len(result.rows)} 行；"
        f"{label}{request.output.label if request.output else output_field}"
        f"={selected.get(output_field)}{unit}{exclusion_note}。"
    )
