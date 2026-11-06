from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import CalculationResult


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {
            str(key): json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def result_payload(result: CalculationResult) -> dict[str, Any]:
    rows = tuple(json_value(dict(row)) for row in result.rows)
    return {
        "accepted": result.accepted,
        "tool": result.tool,
        "operation": result.operation,
        "value": json_value(result.value),
        "rows": rows,
        "columns": list(result.columns),
        "formula": result.formula,
        "calculation": json_value(result.evidence),
    }


def failure_payload(result: CalculationResult) -> dict[str, Any]:
    return {
        "accepted": False,
        "failure_code": result.failure_code,
        "stage": "calculation",
        "tool": result.tool,
        "operation": result.operation,
        "reason": result.reason,
        "evidence": json_value(result.evidence),
        "allowed_actions": ["rewrite_calculation", "ask_user"],
    }
