from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..models import CalculationRequest, CalculationResult


def render_series_calculation(
    result: CalculationResult,
    request: CalculationRequest,
) -> str:
    points = "、".join(
        f"{row.get('label')}={row.get('value')}"
        for row in result.rows
    )
    if not points:
        return "序列计算没有可用数据。"
    change_name = (
        "difference"
        if request.operation == "subtract"
        else "percentage_change"
    )
    unit = (
        request.output.unit
        if request.output and request.output.unit
        else ""
    )
    changes = "，".join(
        f"{format_change(row.get(change_name))}{unit}"
        for row in result.rows[1:]
    )
    change_values = [
        row.get(change_name)
        for row in result.rows[1:]
        if row.get(change_name) is not None
    ]
    if change_values and all(value <= 0 for value in change_values):
        direction = "整体下降"
    elif change_values and all(value >= 0 for value in change_values):
        direction = "整体上升"
    else:
        direction = "有升有降"
    return f"{direction}。序列为：{points}。相邻变化为：{changes}。"


def format_change(value: Any) -> str:
    if value is None:
        return "无"
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(number.normalize(), "f")
    return f"+{text}" if number > 0 else text
