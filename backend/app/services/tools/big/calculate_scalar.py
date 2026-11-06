from __future__ import annotations

from typing import Any

from ....calculations.models import CalculationRequest
from ....calculations.scalar import calculate_scalar as run_calculation
from ....calculations.serialization import failure_payload, result_payload
from ..observation import observation
from ..registry import tool


@tool(
    name="calculate_scalar",
    layer="big",
    domain="calculation",
    description="用注册的标量操作符计算两个数字；操作数使用命名角色。",
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "operands": {"type": "array", "items": {"type": "object"}},
            "precision": {"type": "integer", "default": 6},
        },
        "required": ["operation", "operands"],
    },
    wraps="app.calculations.scalar.calculate_scalar",
)
def calculate_scalar(
    operation: str,
    operands: list[dict[str, Any]],
    precision: int = 6,
) -> dict[str, Any]:
    request = CalculationRequest.from_dict(
        {
            "tool": "calculate_scalar",
            "operation": operation,
            "operands": operands,
            "precision": precision,
        }
    )
    result = run_calculation(request)
    payload = result_payload(result)
    if not result.accepted:
        failure = failure_payload(result)
        return observation(
            "calculate_scalar",
            ok=False,
            summary=result.reason,
            error=result.failure_code,
            next_hint="修正操作符或命名操作数。",
            failure=failure,
        )
    return observation(
        "calculate_scalar",
        ok=True,
        summary=f"{result.formula} = {payload['value']}",
        data={
            "row_count": 1,
            "columns": ["value"],
            "preview_rows": [{"value": payload["value"]}],
            "produced": {"value": payload["value"]},
            "truncated": False,
            "calculation": payload,
        },
        next_hint="使用计算结果继续分析。",
    )
