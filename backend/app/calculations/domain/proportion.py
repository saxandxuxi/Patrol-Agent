from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from ..models import CalculationRequest, CalculationResult
from ..numbers import as_decimal


def is_proportion_calculation(request: CalculationRequest) -> bool:
    """Return whether the structured output contract represents a share."""

    if request.tool != "calculate_columns":
        return False
    if request.operation in {"ratio", "percentage_share"}:
        return True
    return bool(
        request.operation == "divide"
        and request.output is not None
        and request.output.unit.strip() == "成"
    )


def render_proportion_calculation(
    result: CalculationResult,
    request: CalculationRequest,
    output_field: str,
) -> str:
    """Render a proportion with explicit percent and cheng values."""

    rows = [
        row for row in result.rows if isinstance(row, dict)
    ]
    if not rows:
        return "比例计算没有可用结果。"
    label_fields = [
        str(key)
        for key in rows[0]
        if str(key) != output_field
        and any(row.get(key) not in (None, "") for row in rows)
        and all(
            row.get(key) in (None, "")
            or as_decimal(row.get(key)) is None
            for row in rows
        )
    ]
    output_label = (
        request.output.label
        if request.output and request.output.label
        else output_field
    )
    values: list[tuple[str, Decimal]] = []
    for index, row in enumerate(rows, start=1):
        ratio = as_decimal(row.get(output_field))
        if ratio is None:
            continue
        label = "，".join(
            f"{field} {row.get(field)}"
            for field in label_fields
            if row.get(field) not in (None, "")
        )
        if not label:
            label = f"第 {index} 行"
        values.append((label, ratio))
    if not values:
        return "比例计算没有可用结果。"

    def display(value: Decimal) -> str:
        rounded = value.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        return format(rounded, "f").rstrip("0").rstrip(".")

    percent_factor = (
        Decimal("1")
        if request.operation == "percentage_share"
        else Decimal("100")
    )
    cheng_factor = percent_factor / Decimal("10")
    parts = [
        (
            f"{label}的{output_label}为 "
            f"{display(ratio * percent_factor)}%"
            f"（约 {display(ratio * cheng_factor)}成）"
        )
        for label, ratio in values
    ]
    top_label, top_ratio = max(values, key=lambda item: item[1])
    conclusion = (
        f"其中{top_label}的{output_label}最高"
        f"（{display(top_ratio * percent_factor)}%）。"
    )
    return "比例计算结果：" + "；".join(parts) + "。" + conclusion
