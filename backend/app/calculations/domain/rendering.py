from __future__ import annotations

from typing import Any

from ..models import CalculationRequest, CalculationResult
from .density import render_column_calculation
from .trend import render_series_calculation


def render_calculation(
    result: CalculationResult,
    request: CalculationRequest,
    task: dict[str, Any],
    output_field: str,
) -> str:
    if request.tool == "calculate_scalar":
        label = (
            request.output.label
            if request.output and request.output.label
            else request.output.name
            if request.output
            else "计算结果"
        )
        return f"{label} = {result.value}（{result.formula}）。"
    if request.tool == "calculate_series":
        return render_series_calculation(result, request)
    return render_column_calculation(
        result,
        request,
        task,
        output_field,
    )
