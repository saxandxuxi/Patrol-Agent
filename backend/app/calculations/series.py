from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import CalculationResult
from .numbers import as_decimal, quantize
from .registry import default_calculation_registry


def calculate_series(
    operation: str,
    labels: list[Any] | tuple[Any, ...],
    values: list[Any] | tuple[Any, ...],
    *,
    precision: int = 6,
) -> CalculationResult:
    base = CalculationResult(
        accepted=False,
        tool="calculate_series",
        operation=operation,
    )
    capability = default_calculation_registry().capability("calculate_series")
    if capability is None:
        return base.failure(
            "calculation_tool_unknown",
            "未注册计算工具：calculate_series",
        )
    if operation not in capability.operations:
        return base.failure(
            "calculation_operation_unknown",
            f"calculate_series 不支持操作符 {operation}",
            allowed_operations=list(capability.operations),
        )
    if len(labels) != len(values) or not values:
        return base.failure(
            "calculation_operand_missing",
            "序列标签与数值数量不一致或为空",
            label_count=len(labels),
            value_count=len(values),
        )
    parsed: list[Decimal] = []
    invalid_indexes: list[int] = []
    for index, value in enumerate(values):
        number = as_decimal(value)
        if number is None:
            invalid_indexes.append(index)
        else:
            parsed.append(number)
    if invalid_indexes:
        return base.failure(
            "calculation_type_mismatch",
            "序列中包含非数字",
            indexes=invalid_indexes,
        )
    output_name = (
        "difference"
        if operation == "subtract"
        else "percentage_change"
    )
    rows: list[dict[str, Any]] = []
    for index, (label, value) in enumerate(zip(labels, parsed)):
        result_value: Decimal | None = None
        if index:
            previous = parsed[index - 1]
            if operation == "subtract":
                result_value = value - previous
            else:
                if previous == 0:
                    return base.failure(
                        "calculation_zero_denominator",
                        "百分比变化的上一批次为 0",
                        label=str(label),
                        index=index,
                    )
                result_value = (
                    (value - previous) / previous * Decimal("100")
                )
            result_value = quantize(
                result_value,
                precision or capability.precision,
            )
        rows.append(
            {
                "label": label,
                "value": value,
                output_name: result_value,
            }
        )
    return CalculationResult(
        accepted=True,
        tool="calculate_series",
        operation=operation,
        rows=tuple(rows),
        columns=("label", "value", output_name),
        formula=(
            "current - previous"
            if operation == "subtract"
            else "(current - previous) / previous * 100"
        ),
        evidence={
            "precision": precision or capability.precision,
            "point_count": len(rows),
        },
    )
