from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..services.resource_loader import load_yaml


def _config() -> dict[str, Any]:
    value = load_yaml("capabilities/preset_bindings.yaml", {})
    return value if isinstance(value, dict) else {}


def _base_field(field: str) -> str:
    return str(field or "").rsplit(".", 1)[-1]


def filter_semantic_name(field: str) -> str:
    normalized = _base_field(field)
    return str((_config().get("filters") or {}).get(normalized) or normalized)


def preset_parameter_name(field: str) -> str:
    normalized = _base_field(field)
    return str(
        (_config().get("parameter_aliases") or {}).get(normalized)
        or normalized
    )


def is_derived_parameter(field: str) -> bool:
    normalized = _base_field(field)
    return normalized in {
        str(item)
        for item in _config().get("derived_parameters") or ()
    }


def _time_granularity(spec: Any) -> str:
    for dimension in getattr(spec, "dimensions", ()) or ():
        field = _base_field(dimension)
        for granularity in ("day", "week", "month", "quarter", "year"):
            if field == granularity or field.endswith(f"_{granularity}"):
                return granularity
    return "day"


def _date_part(value: Any) -> str:
    return str(value or "").strip()[:10]


def values_from_spec(spec: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    values["date_granularity"] = _time_granularity(spec)
    if spec.limit is not None:
        values[preset_parameter_name("limit")] = str(spec.limit)
    time_fields = set(_config().get("time_fields") or ())
    time_lower: tuple[Any, bool] | None = None
    time_upper: tuple[Any, bool] | None = None
    time_equals: Any | None = None
    for binding in spec.filters:
        field = _base_field(binding.field)
        if field in time_fields:
            if binding.op == "between" and isinstance(
                binding.storage_value,
                (list, tuple),
            ) and len(binding.storage_value) == 2:
                time_lower = (binding.storage_value[0], True)
                time_upper = (binding.storage_value[1], False)
            elif binding.op in {">", ">="}:
                time_lower = (binding.storage_value, binding.op == ">=")
            elif binding.op in {"<", "<="}:
                time_upper = (binding.storage_value, binding.op == "<=")
            elif binding.op == "=":
                time_equals = binding.storage_value
            continue
        value = binding.storage_value
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        values[preset_parameter_name(field)] = str(value)
    if time_equals is not None:
        day = _date_part(time_equals)
        values.update(
            {
                "date_from": day,
                "date_to": day,
            }
        )
    elif time_lower is not None and time_upper is not None:
        start = _date_part(time_lower[0])
        try:
            end_value = datetime.fromisoformat(str(time_upper[0]))
            if not time_upper[1]:
                end_value -= timedelta(days=1)
            end = end_value.date().isoformat()
        except ValueError:
            end = _date_part(time_upper[0])
        values.update(
            {
                "date_from": start,
                "date_to": end,
            }
        )
    return values


def apply_overrides(values: dict[str, str], overrides: dict[str, Any]) -> None:
    for key, value in overrides.items():
        values[preset_parameter_name(key)] = str(value)
