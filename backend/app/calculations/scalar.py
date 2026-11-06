from __future__ import annotations

from decimal import Decimal

from .models import CalculationRequest, CalculationResult
from .numbers import as_decimal, quantize
from .registry import default_calculation_registry


_ROLES = {
    "add": ("left", "right"),
    "subtract": ("left", "right"),
    "multiply": ("left", "right"),
    "divide": ("numerator", "denominator"),
    "ratio": ("numerator", "denominator"),
    "percentage_share": ("numerator", "total"),
    "percentage_change": ("old", "new"),
}


def _base(request: CalculationRequest) -> CalculationResult:
    return CalculationResult(
        accepted=False,
        tool=request.tool,
        operation=request.operation,
    )


def calculate_scalar(request: CalculationRequest) -> CalculationResult:
    result = _base(request)
    capability = default_calculation_registry().capability(request.tool)
    if capability is None:
        return result.failure(
            "calculation_tool_unknown",
            f"未注册计算工具：{request.tool}",
            tool=request.tool,
        )
    if request.operation not in capability.operations:
        return result.failure(
            "calculation_operation_unknown",
            f"工具 {request.tool} 不支持操作符 {request.operation}",
            operation=request.operation,
            allowed_operations=list(capability.operations),
        )
    roles = _ROLES.get(request.operation)
    if roles is None:
        return result.failure(
            "calculation_operation_unknown",
            f"未实现操作符：{request.operation}",
            operation=request.operation,
        )
    by_name = {item.name: item for item in request.operands}
    missing = [name for name in roles if name not in by_name]
    if missing:
        return result.failure(
            "calculation_operand_missing",
            "缺少计算操作数：" + "、".join(missing),
            missing=missing,
            required=list(roles),
        )
    values = {
        name: as_decimal(by_name[name].value)
        for name in roles
    }
    invalid = [name for name, value in values.items() if value is None]
    if invalid:
        return result.failure(
            "calculation_type_mismatch",
            "计算操作数不是有效数字：" + "、".join(invalid),
            invalid=invalid,
        )
    left = values[roles[0]]
    right = values[roles[1]]
    assert left is not None and right is not None
    if request.operation == "add":
        raw = left + right
        formula = f"{left} + {right}"
    elif request.operation == "subtract":
        raw = left - right
        formula = f"{left} - {right}"
    elif request.operation == "multiply":
        raw = left * right
        formula = f"{left} * {right}"
    elif request.operation in {"divide", "ratio"}:
        if right == 0:
            return result.failure(
                "calculation_zero_denominator",
                "除数不能为 0",
                zero_operand=roles[1],
            )
        raw = left / right
        formula = f"{left} / {right}"
    elif request.operation == "percentage_share":
        if right == 0:
            return result.failure(
                "calculation_zero_denominator",
                "占比总量不能为 0",
                zero_operand=roles[1],
            )
        raw = left / right * Decimal("100")
        formula = f"{left} / {right} * 100"
    else:
        if left == 0:
            return result.failure(
                "calculation_zero_denominator",
                "百分比变化的旧值不能为 0",
                zero_operand=roles[0],
            )
        old = left
        new = right
        raw = (new - old) / old * Decimal("100")
        formula = f"({new} - {old}) / {old} * 100"
    return CalculationResult(
        accepted=True,
        tool=request.tool,
        operation=request.operation,
        value=quantize(raw, request.precision or capability.precision),
        formula=formula,
        evidence={
            "operands": {
                name: str(values[name]) for name in roles
            },
            "precision": request.precision or capability.precision,
        },
    )
