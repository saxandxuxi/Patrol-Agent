from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


ROLE_NAMES = {
    "add": ("left", "right"),
    "subtract": ("left", "right"),
    "multiply": ("left", "right"),
    "divide": ("numerator", "denominator"),
    "ratio": ("numerator", "denominator"),
    "percentage_share": ("numerator", "total"),
    "percentage_change": ("old", "new"),
}


def roles_for_tool(tool: str, operation: str) -> tuple[str, ...]:
    if tool == "calculate_series":
        return ("labels", "values")
    return ROLE_NAMES.get(operation, ())


@dataclass(frozen=True)
class OperationResult:
    value: Decimal | None = None
    formula: str = ""
    failure_code: str = ""
    reason: str = ""
    zero_operand: str = ""


def evaluate(operation: str, values: dict[str, Decimal]) -> OperationResult:
    roles = ROLE_NAMES.get(operation)
    if roles is None:
        return OperationResult(
            failure_code="calculation_operation_unknown",
            reason=f"未实现操作符：{operation}",
        )
    left = values[roles[0]]
    right = values[roles[1]]
    if operation == "add":
        return OperationResult(left + right, f"{left} + {right}")
    if operation == "subtract":
        return OperationResult(left - right, f"{left} - {right}")
    if operation == "multiply":
        return OperationResult(left * right, f"{left} * {right}")
    if operation in {"divide", "ratio"}:
        if right == 0:
            return OperationResult(
                failure_code="calculation_zero_denominator",
                reason="除数不能为 0",
                zero_operand=roles[1],
            )
        return OperationResult(left / right, f"{left} / {right}")
    if operation == "percentage_share":
        if right == 0:
            return OperationResult(
                failure_code="calculation_zero_denominator",
                reason="占比总量不能为 0",
                zero_operand=roles[1],
            )
        return OperationResult(
            left / right * Decimal("100"),
            f"{left} / {right} * 100",
        )
    if left == 0:
        return OperationResult(
            failure_code="calculation_zero_denominator",
            reason="百分比变化的旧值不能为 0",
            zero_operand=roles[0],
        )
    old = left
    new = right
    return OperationResult(
        (new - old) / old * Decimal("100"),
        f"({new} - {old}) / {old} * 100",
    )
