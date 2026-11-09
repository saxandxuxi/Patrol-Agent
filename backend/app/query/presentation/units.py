from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..capability_registry import CapabilityRegistry


_COUNT_UNITS = {
    "disease": "处",
    "patrol": "次",
    "route": "条",
    "section": "段",
    "work_order": "条",
    "bridge": "座",
    "tunnel": "座",
    "slope": "处",
}


def _spec_value(spec: Any, key: str, default: Any) -> Any:
    if isinstance(spec, Mapping):
        return spec.get(key, default)
    return getattr(spec, key, default)


def display_unit(unit: str) -> str:
    return {
        "km": "公里",
        "m": "米",
    }.get(str(unit or "").strip().lower(), str(unit or "").strip())


def metric_unit(key: str, subject: str) -> str:
    key_text = str(key)
    lowered = key_text.lower()
    if "_km" in lowered or "公里" in key_text or "里程" in key_text:
        return "公里"
    if "面积" in key_text or "area" in lowered:
        return "㎡"
    if "长度" in key_text or "length" in lowered:
        return "米"
    if (
        key_text in {"数量", "处数", "记录数"}
        or lowered.endswith("_count")
        or lowered == "count"
    ):
        return _COUNT_UNITS.get(subject, "条")
    return ""


def registered_units(
    spec: Any,
    registry: CapabilityRegistry | None = None,
) -> dict[str, str]:
    """Resolve display units from the same capability facts used by planning."""

    if spec is None:
        return {}
    if registry is None:
        try:
            registry = CapabilityRegistry.load(
                str(_spec_value(spec, "kind", "mysql") or "mysql")
            )
        except Exception:  # noqa: BLE001
            return {}
    subject = str(_spec_value(spec, "subject", "") or "")
    units: dict[str, str] = {}
    metric_group = registry.snapshot.metrics.get(subject, {})
    for metric_id in _spec_value(spec, "metrics", ()) or ():
        metric = metric_group.get(str(metric_id))
        if metric is None or not metric.unit:
            continue
        unit = display_unit(metric.unit)
        units[str(metric_id)] = unit
        units[metric.label] = unit
    field_group = registry.snapshot.fields.get(subject, {})
    for field_id in _spec_value(spec, "output_projection", ()) or ():
        field = field_group.get(str(field_id))
        if field is None or not field.unit:
            continue
        unit = display_unit(field.unit)
        units[str(field_id)] = unit
        units[field.label] = unit
    return units
