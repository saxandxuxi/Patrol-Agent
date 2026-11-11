from __future__ import annotations

from typing import Any

from ..models import CalculationRequest, CalculationResult
from ..numbers import as_decimal


def render_group_change_calculation(
    result: CalculationResult,
    request: CalculationRequest,
) -> str:
    evidence = result.evidence if isinstance(result.evidence, dict) else {}
    label_field = str(evidence.get("label_field") or "")
    output_field = str(evidence.get("output_field") or "变化值")
    output_label = (
        request.output.label
        if request.output is not None and request.output.label
        else request.output.name
        if request.output is not None
        else output_field
    )
    unit = (
        request.output.unit
        if request.output is not None and request.output.unit
        else ""
    )
    comparable: list[tuple[dict[str, Any], Any]] = []
    for row in result.rows:
        value = as_decimal(row.get(output_field))
        if value is not None:
            comparable.append((row, value))
    if not comparable:
        return (
            f"前后两次分组结果没有可定义的{output_label}；"
            "前次值为 0 的分组不能计算百分比变化。"
        )

    maximum = max(value for _, value in comparable)
    leaders = [
        str(row.get(label_field) or "")
        for row, value in comparable
        if value == maximum
    ]
    leader_text = "、".join(item for item in leaders if item) or "无法识别分组"
    unit_text = f" {unit}" if unit else ""
    basis = (
        f"{leaders[0]}由 {_row_value(result.rows, label_field, leaders[0], '前次值')}"
        f" 变为 {_row_value(result.rows, label_field, leaders[0], '后次值')}"
        if len(leaders) == 1
        else "这些分组的变化值相同"
    )
    if request.operation == "percentage_change":
        if maximum > 0:
            return (
                f"{leader_text}的{output_label}最高，为"
                f"{maximum:g}%；{basis}。"
            )
        return (
            f"没有分组的{output_label}为正数；最高变化为"
            f"{maximum:g}%；{basis}。"
        )
    if maximum > 0:
        return (
            f"{leader_text}增加最多，{output_label}为"
            f"{maximum:g}{unit_text}；{basis}。"
        )
    return (
        f"没有分组数量增加；变化值最高的是{leader_text}，"
        f"为{maximum:g}{unit_text}；{basis}。"
    )


def _row_value(
    rows: tuple[dict[str, Any], ...],
    label_field: str,
    label: str,
    field: str,
) -> str:
    for row in rows:
        if str(row.get(label_field) or "") == label:
            value = as_decimal(row.get(field))
            return f"{value:g}" if value is not None else "未知"
    return "未知"
