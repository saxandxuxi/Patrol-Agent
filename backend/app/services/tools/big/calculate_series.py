from __future__ import annotations

from typing import Any

from ....calculations.serialization import failure_payload, result_payload
from ....calculations.series import calculate_series as run_calculation
from ..observation import observation
from ..registry import tool


@tool(
    name="calculate_series",
    layer="big",
    domain="calculation",
    description="计算有序批次数列的相邻差值或百分比变化，并保留完整批次轴。",
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string"},
            "labels": {"type": "array"},
            "values": {"type": "array"},
            "precision": {"type": "integer", "default": 6},
        },
        "required": ["operation", "labels", "values"],
    },
    wraps="app.calculations.series.calculate_series",
)
def calculate_series(
    operation: str,
    labels: list[Any],
    values: list[Any],
    precision: int = 6,
) -> dict[str, Any]:
    result = run_calculation(
        operation,
        labels,
        values,
        precision=precision,
    )
    payload = result_payload(result)
    if not result.accepted:
        failure = failure_payload(result)
        return observation(
            "calculate_series",
            ok=False,
            summary=result.reason,
            error=result.failure_code,
            next_hint="检查批次标签、数值和零值基线。",
            failure=failure,
        )
    return observation(
        "calculate_series",
        ok=True,
        summary=f"已计算 {len(result.rows)} 个批次点。",
        data={
            "row_count": len(result.rows),
            "columns": list(result.columns),
            "preview_rows": list(payload["rows"][:10]),
            "produced": {},
            "truncated": False,
            "calculation": payload,
        },
        next_hint="使用序列变化结果继续分析。",
    )
