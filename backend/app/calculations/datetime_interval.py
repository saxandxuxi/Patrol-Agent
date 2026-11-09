from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .models import CalculationRequest, CalculationResult


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def calculate_datetime_interval(
    request: CalculationRequest,
) -> CalculationResult:
    if (
        request.tool != "calculate_datetime_interval"
        or request.operation != "elapsed_days"
    ):
        return CalculationResult(
            accepted=False,
            tool=request.tool,
            operation=request.operation,
            failure_code="calculation_operation_unknown",
            reason="日期差工具只支持 elapsed_days",
        )
    values = {item.name: item.value for item in request.operands}
    earlier = _parse_datetime(values.get("earlier"))
    later = _parse_datetime(values.get("later"))
    if earlier is None or later is None:
        return CalculationResult(
            accepted=False,
            tool=request.tool,
            operation=request.operation,
            failure_code="calculation_datetime_invalid",
            reason="巡检时间无法解析为有效日期时间",
            evidence={
                "earlier": values.get("earlier"),
                "later": values.get("later"),
            },
        )
    if later < earlier:
        return CalculationResult(
            accepted=False,
            tool=request.tool,
            operation=request.operation,
            failure_code="calculation_datetime_order",
            reason="later 时间不能早于 earlier 时间",
            evidence={
                "earlier": earlier.isoformat(sep=" "),
                "later": later.isoformat(sep=" "),
            },
        )
    quantum = Decimal("1").scaleb(-request.precision)
    days = (
        Decimal(str((later - earlier).total_seconds())) / Decimal("86400")
    ).quantize(quantum, rounding=ROUND_HALF_UP)
    return CalculationResult(
        accepted=True,
        tool=request.tool,
        operation=request.operation,
        value=days,
        formula="later - earlier",
        evidence={
            "earlier": earlier.isoformat(sep=" "),
            "later": later.isoformat(sep=" "),
            "unit": "day",
        },
    )
