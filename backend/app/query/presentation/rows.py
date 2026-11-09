from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..capability_registry import CapabilityRegistry
from ..result_projection import result_field_map
from .units import metric_unit


def is_missing_display_value(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().upper() in {"", "NULL", "NONE"}


def format_display_value(
    value: Any,
    *,
    missing_label: str = "无有效数值",
) -> str:
    """Remove meaningless database trailing zeroes without losing precision."""

    if is_missing_display_value(value):
        return missing_label
    text = str(value).strip()
    if "." not in text:
        return text
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite():
        return text
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized == "-0" else normalized


def format_row_text(
    row: dict[str, Any],
    *,
    subject: str = "",
    units: dict[str, str] | None = None,
    missing_labels: dict[str, str] | None = None,
) -> str:
    parts: list[str] = []
    for key, value in row.items():
        missing_label = (missing_labels or {}).get(str(key))
        display = format_display_value(
            value,
            missing_label=missing_label or "无有效数值",
        )
        unit = ""
        if display != (missing_label or "无有效数值"):
            unit = (units or {}).get(str(key)) or metric_unit(
                str(key),
                subject,
            )
        if unit and not display.endswith(unit):
            display += unit
        parts.append(f"{key} {display}")
    return "，".join(parts)


def missing_value_labels(
    spec: Any,
    columns: tuple[str, ...] | list[str],
    registry: CapabilityRegistry | None = None,
) -> dict[str, str]:
    """Label null joined dimensions as missing relations, not numeric zero."""

    if spec is None:
        return {}
    registry = registry or CapabilityRegistry.load(
        str(getattr(spec, "kind", "mysql") or "mysql")
    )
    subject = str(getattr(spec, "subject", "") or "")
    fields = registry.snapshot.fields.get(subject, {})
    labels: dict[str, str] = {}
    for field, column in result_field_map(spec, columns).items():
        capability = fields.get(field)
        if (
            capability is None
            or not capability.requires_join
            or "dimension" not in capability.roles
        ):
            continue
        labels[str(column)] = f"未关联{capability.label}"
    return labels


def empty_metric_note(
    rows: list[dict[str, Any]],
    *,
    missing_labels: dict[str, str] | None = None,
) -> str:
    """State that a missing metric is empty, not zero."""

    for row in rows:
        if any(
            is_missing_display_value(value)
            and str(key) not in (missing_labels or {})
            for key, value in row.items()
        ):
            return "有个别指标没有有效数值（不是 0），结果里显示为“无有效数值”"
    return ""
