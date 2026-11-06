from __future__ import annotations

from .models import CalculationRequest, CalculationResult
from .numbers import as_decimal, quantize
from .operations import ROLE_NAMES, evaluate
from .registry import default_calculation_registry


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
    roles = ROLE_NAMES.get(request.operation)
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
    typed_values = {
        name: value for name, value in values.items() if value is not None
    }
    operation_result = evaluate(request.operation, typed_values)
    if operation_result.failure_code:
        return result.failure(
            operation_result.failure_code,
            operation_result.reason,
            zero_operand=operation_result.zero_operand,
        )
    raw = operation_result.value
    assert raw is not None
    return CalculationResult(
        accepted=True,
        tool=request.tool,
        operation=request.operation,
        value=quantize(raw, request.precision or capability.precision),
        formula=operation_result.formula,
        evidence={
            "operands": {
                name: str(values[name]) for name in roles
            },
            "precision": request.precision or capability.precision,
        },
    )
