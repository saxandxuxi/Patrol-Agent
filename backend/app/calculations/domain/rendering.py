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
    if request.tool == "calculate_datetime_interval":
        label = (
            request.output.label
            if request.output and request.output.label
            else request.output.name
            if request.output
            else "时间间隔"
        )
        unit = (
            request.output.unit
            if request.output and request.output.unit
            else "天"
        )
        evidence = result.evidence if isinstance(result.evidence, dict) else {}
        earlier = str(evidence.get("earlier") or "")
        later = str(evidence.get("later") or "")
        detail = (
            f"（{earlier} 至 {later}）"
            if earlier and later
            else ""
        )
        return f"{label} = {result.value:g} {unit}{detail}。"
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


def render_calculation_failure(failure: Mapping[str, Any] | None) -> str:
    """Turn structured calculation evidence into a user-facing failure reason."""
    payload = failure if isinstance(failure, Mapping) else {}
    code = str(payload.get("failure_code") or "")
    evidence = payload.get("evidence")
    calculation = (
        evidence.get("calculation")
        if isinstance(evidence, Mapping)
        else None
    )
    details = calculation if isinstance(calculation, Mapping) else {}
    if code != "calculation_zero_denominator":
        return ""
    zero_operand = str(details.get("zero_operand") or "")
    operands = details.get("operands")
    operand = next(
        (
            item
            for item in (operands if isinstance(operands, list) else ())
            if isinstance(item, Mapping)
            and str(item.get("name") or "") == zero_operand
        ),
        {},
    )
    label = str(operand.get("label") or "").strip()
    subject = f"{label}为 0" if label else "作为分母的数据为 0"
    operation = str(details.get("operation") or "")
    if operation == "percentage_share":
        return f"{subject}，占比无法定义。"
    if operation == "percentage_change":
        return f"{subject}，百分比变化无法定义。"
    return f"{subject}，平均值无法定义，密度或比值也无法计算。"


def _difference_conclusion(
    result: CalculationResult,
    labels: Mapping[str, str],
) -> str:
    """差值要有结论：谁更多，以及从前者到后者是增加还是减少。

    只给“-13145（10872 - 24017）”这种公式，用户看不出哪一侧更多，
    而且“相差多少”要的是绝对值。上游节点的说明能确定谁更大时直接点名；
    不确定时至少给出正值、两侧数值和方向，不能输出负的“相差”。
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
    difference = abs(left - right)
    left_label = str(labels.get("left") or "").strip() or "左侧"
    right_label = str(labels.get("right") or "").strip() or "右侧"
    direction = "多" if left > right else "少"
    return (
        f"{difference:g}"
        f"（{left_label} {left:g} vs {right_label} {right:g}；"
        f"相差 {difference:g}；"
        f"{left_label}比{right_label}{direction} {difference:g}）"
    )
