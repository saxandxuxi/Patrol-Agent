from __future__ import annotations

from typing import Any

from ....calculations.columns import calculate_columns as run_calculation
from ....calculations.serialization import failure_payload, result_payload
from ..observation import observation
from ..registry import tool


@tool(
    name="calculate_columns",
    layer="big",
    domain="calculation",
    description="对前序结果的两列逐行计算，并保留原行 ID 和展示字段。",
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "rows": {"type": "array", "items": {"type": "object"}},
            "operands": {"type": "object"},
            "output_field": {"type": "string"},
            "precision": {"type": "integer", "default": 6},
        },
        "required": ["operation", "rows", "operands", "output_field"],
    },
    wraps="app.calculations.columns.calculate_columns",
)
def calculate_columns(
    operation: str,
    rows: list[dict[str, Any]],
    operands: dict[str, str],
    output_field: str,
    precision: int = 6,
    zero_denominator: str = "error",
) -> dict[str, Any]:
    result = run_calculation(
        operation,
        rows,
        operands,
        output_field,
        precision=precision,
        zero_denominator=zero_denominator,
    )
    payload = result_payload(result)
    if not result.accepted:
        failure = failure_payload(result)
        return observation(
            "calculate_columns",
            ok=False,
            summary=result.reason,
            error=result.failure_code,
            next_hint="检查列名、行数字和除数列。",
            failure=failure,
        )
    return observation(
        "calculate_columns",
        ok=True,
        summary=f"已生成 {len(result.rows)} 行计算结果。",
        data={
            "row_count": len(result.rows),
            "columns": list(result.columns),
            "preview_rows": list(payload["rows"][:10]),
            "produced": {output_field: "computed"},
            "truncated": False,
            "calculation": payload,
        },
        next_hint="使用计算列或继续分析。",
    )
