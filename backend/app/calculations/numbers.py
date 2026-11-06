from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


def as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def quantize(value: Decimal, precision: int) -> Decimal:
    places = max(0, min(int(precision), 12))
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def numeric_value(
    dependency: dict[str, Any],
    field: str = "",
) -> Decimal | None:
    if field:
        produced = dependency.get("produced") or {}
        value = as_decimal(produced.get(field))
        if value is not None:
            return value
    for row in dependency.get("rows") or []:
        if not isinstance(row, dict):
            continue
        for name, item in row.items():
            if field and field not in str(name):
                continue
            value = as_decimal(item)
            if value is not None:
                return value
    return None
