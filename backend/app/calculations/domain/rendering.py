from __future__ import annotations

from typing import Any, Mapping

from ..models import CalculationRequest, CalculationResult
from ..numbers import as_decimal
from .density import render_column_calculation
from .trend import render_series_calculation


def render_calculation(
    result: CalculationResult,
    request: CalculationRequest,
    task: dict[str, Any],
    output_field: str,
    operand_labels: Mapping[str, str] | None = None,
) -> str:
    if request.tool == "calculate_scalar":
        label = (
            request.output.label
            if request.output and request.output.label
            else request.output.name
            if request.output
            else "计算结果"
        )
        conclusion = (
            _difference_conclusion(result, operand_labels or {})
            if request.operation == "subtract"
            else ""
        )
        if conclusion:
            return f"{label} = {conclusion}。"
        return f"{label} = {result.value}（{result.formula}）。"
    if request.tool == "calculate_series":
        return render_series_calculation(result, request)
    return render_column_calculation(
        result,
        request,
        task,
        output_field,
    )


def _difference_conclusion(
    result: CalculationResult,
    labels: Mapping[str, str],
) -> str:
    """差值要有结论：谁更多、多多少。

    只给“-13145（10872 - 24017）”这种公式，用户看不出哪一侧更多，
    而且“相差多少”要的是绝对值。上游节点的说明能确定谁更大时直接点名；
    不确定时至少给出正值和两侧数值，不能输出负的“相差”。
    """
    evidence = result.evidence if isinstance(result.evidence, dict) else {}
    operands = evidence.get("operands")
    if not isinstance(operands, Mapping):
        return ""
    left = as_decimal(operands.get("left"))
    right = as_decimal(operands.get("right"))
    if left is None or right is None:
        return ""
    if left == right:
        return f"{left:g}（两侧一样多）"
    bigger, smaller = (left, right) if left > right else (right, left)
    bigger_label = str(
        labels.get("left" if left > right else "right") or ""
    ).strip()
    difference = abs(left - right)
    if bigger_label:
        return (
            f"{difference:g}"
            f"（{bigger_label}更多：{bigger:g} vs {smaller:g}）"
        )
    return f"{difference:g}（{bigger:g} vs {smaller:g} 相差 {difference:g}）"
