from __future__ import annotations

from typing import Any

from .models import CalculationResult
from .numbers import as_decimal, quantize
from .operations import ROLE_NAMES, evaluate
from .registry import default_calculation_registry


def _row_id(row: dict[str, Any], index: int) -> str:
    for key in ("id", "route_name", "patrol_number", "problem_number"):
        if row.get(key) not in (None, ""):
            return str(row[key])
    for key, value in row.items():
        text = str(key).lower()
        if value not in (None, "") and (
            text.endswith("_id")
            or "编号" in text
            or "名称" in text
        ):
            return str(value)
    return str(index)


def calculate_columns(
    operation: str,
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    operands: dict[str, str],
    output_field: str,
    *,
    precision: int = 6,
    zero_denominator: str = "error",
    constants: dict[str, Any] | None = None,
) -> CalculationResult:
    base = CalculationResult(
        accepted=False,
        tool="calculate_columns",
        operation=operation,
    )
    capability = default_calculation_registry().capability("calculate_columns")
    if capability is None:
        return base.failure(
            "calculation_tool_unknown",
            "未注册计算工具：calculate_columns",
        )
    if operation not in capability.operations:
        return base.failure(
            "calculation_operation_unknown",
            f"calculate_columns 不支持操作符 {operation}",
            allowed_operations=list(capability.operations),
        )
    roles = ROLE_NAMES.get(operation)
    if roles is None:
        return base.failure(
            "calculation_operation_unknown",
            f"未实现操作符：{operation}",
        )
    constant_values = dict(constants or {})
    missing_roles = [
        role
        for role in roles
        if not operands.get(role) and role not in constant_values
    ]
    if missing_roles:
        return base.failure(
            "calculation_operand_missing",
            "缺少列计算角色：" + "、".join(missing_roles),
            missing=missing_roles,
            required=list(roles),
        )
    source_rows = [dict(row) for row in rows if isinstance(row, dict)]
    if not source_rows:
        return base.failure(
            "calculation_operand_missing",
            "列计算没有输入行",
        )
    available = set(source_rows[0])
    missing_columns = [
        operands[role]
        for role in roles
        if role not in constant_values and operands[role] not in available
    ]
    if not output_field:
        missing_columns.append("output_field")
    if missing_columns:
        return base.failure(
            "calculation_operand_missing",
            "缺少计算列：" + "、".join(missing_columns),
            missing_columns=missing_columns,
        )
    output_rows: list[dict[str, Any]] = []
    row_ids: list[str] = []
    excluded_row_ids: list[str] = []
    invalid_rows: list[str] = []
    formula = ""
    for index, row in enumerate(source_rows):
        values = {
            role: as_decimal(
                constant_values[role]
                if role in constant_values
                else row.get(operands[role])
            )
            for role in roles
        }
        if any(value is None for value in values.values()):
            invalid_rows.append(_row_id(row, index))
            continue
        typed = {
            role: value for role, value in values.items() if value is not None
        }
        operation_result = evaluate(operation, typed)
        if operation_result.failure_code == "calculation_zero_denominator":
            if zero_denominator in {"skip_rows", "skip"}:
                excluded_row_ids.append(_row_id(row, index))
            else:
                row_ids.append(_row_id(row, index))
            continue
        if operation_result.failure_code or operation_result.value is None:
            return base.failure(
                operation_result.failure_code or "calculation_output_invalid",
                operation_result.reason or "列计算没有产出有效结果",
                row_id=_row_id(row, index),
            )
        formula = operation_result.formula
        output_rows.append(
            {
                **row,
                output_field: quantize(
                    operation_result.value,
                    precision,
                ),
            }
        )
    if row_ids:
        return base.failure(
            "calculation_zero_denominator",
            "部分行的除数为 0",
            row_ids=row_ids,
            operand=operands[roles[1]],
        )
    if invalid_rows:
        return base.failure(
            "calculation_type_mismatch",
            "部分行不是有效数字",
            row_ids=invalid_rows,
        )
    if not output_rows:
        return base.failure(
            "calculation_output_invalid",
            "列计算没有可输出的有效行",
            excluded_row_ids=excluded_row_ids,
        )
    return CalculationResult(
        accepted=True,
        tool="calculate_columns",
        operation=operation,
        rows=tuple(output_rows),
        columns=tuple(dict.fromkeys([*source_rows[0], output_field])),
        formula=formula,
        evidence={
            "operands": dict(operands),
            "output_field": output_field,
            "precision": precision,
            "excluded_row_ids": excluded_row_ids,
        },
    )
