from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import CalculationResult
from .numbers import as_decimal, quantize
from .registry import default_calculation_registry


def calculate_group_change(
    operation: str,
    before_rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    after_rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    label_field: str,
    before_field: str,
    after_field: str,
    output_field: str,
    precision: int = 6,
) -> CalculationResult:
    base = CalculationResult(
        accepted=False,
        tool="calculate_group_change",
        operation=operation,
    )
    capability = default_calculation_registry().capability(
        "calculate_group_change"
    )
    if capability is None:
        return base.failure(
            "calculation_tool_unknown",
            "未注册计算工具：calculate_group_change",
        )
    if operation not in capability.operations:
        return base.failure(
            "calculation_operation_unknown",
            f"calculate_group_change 不支持操作符 {operation}",
            allowed_operations=list(capability.operations),
        )
    if not label_field or not before_field or not after_field or not output_field:
        return base.failure(
            "calculation_operand_missing",
            "分组变化计算缺少标签、前后数值或输出字段",
        )

    before_values, before_error = _index_rows(
        before_rows,
        label_field=label_field,
        value_field=before_field,
    )
    if before_error:
        return base.failure(
            "calculation_operand_missing",
            f"前次结果无法按 {label_field} 建立索引：{before_error}",
        )
    after_values, after_error = _index_rows(
        after_rows,
        label_field=label_field,
        value_field=after_field,
    )
    if after_error:
        return base.failure(
            "calculation_operand_missing",
            f"后次结果无法按 {label_field} 建立索引：{after_error}",
        )

    labels = tuple(dict.fromkeys((*before_values, *after_values)))
    if not labels:
        return base.failure(
            "calculation_operand_missing",
            "前后两次分组结果都没有可比较的分组值",
        )
    missing_before = tuple(
        label for label in labels if label not in before_values
    )
    missing_after = tuple(
        label for label in labels if label not in after_values
    )
    undefined_points: list[str] = []
    rows: list[dict[str, Any]] = []
    for label in labels:
        before = before_values.get(label, Decimal("0"))
        after = after_values.get(label, Decimal("0"))
        if operation == "subtract":
            changed: Decimal | None = after - before
        elif before == 0:
            changed = None
            undefined_points.append(label)
        else:
            changed = (after - before) / before * Decimal("100")
        rows.append(
            {
                label_field: label,
                "前次值": before,
                "后次值": after,
                output_field: (
                    quantize(changed, precision)
                    if changed is not None
                    else None
                ),
            }
        )
    return CalculationResult(
        accepted=True,
        tool="calculate_group_change",
        operation=operation,
        rows=tuple(rows),
        columns=(label_field, "前次值", "后次值", output_field),
        formula=(
            "after - before"
            if operation == "subtract"
            else "(after - before) / before * 100"
        ),
        evidence={
            "label_field": label_field,
            "before_field": before_field,
            "after_field": after_field,
            "output_field": output_field,
            "missing_before": missing_before,
            "missing_after": missing_after,
            "undefined_points": tuple(undefined_points),
            "precision": precision,
        },
    )


def _index_rows(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    label_field: str,
    value_field: str,
) -> tuple[dict[str, Decimal], str]:
    indexed: dict[str, Decimal] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        label = str(row.get(label_field) or "").strip()
        if not label:
            return {}, f"第 {index + 1} 行缺少分组值"
        value = as_decimal(row.get(value_field))
        if value is None:
            return {}, f"分组 {label} 的字段 {value_field} 不是有效数字"
        indexed[label] = value
    return indexed, ""
