from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ...capability_registry.models import CapabilitySnapshot
from ..models import CapabilityContext, MdlQueryContract


def build_capability_context(
    contract: MdlQueryContract,
    capability_snapshot: CapabilitySnapshot,
) -> CapabilityContext:
    subject = contract.subject
    fields_by_id = capability_snapshot.fields.get(subject, {})
    field_ids = _requested_field_ids(contract)
    fields: dict[str, Mapping[str, Any]] = {}
    enum_values: dict[str, Mapping[str, Mapping[str, Any]]] = {}
    filters_by_field: dict[str, list[Any]] = {}
    for item in contract.filters:
        filters_by_field.setdefault(item.field_id, []).append(item.value)

    include_complete_enums = (
        "complete_enum_dimensions"
        in contract.required_assertions
    )
    complete_enum_fields = {
        field_id
        for field_id in contract.dimensions
        if (
            field_id in fields_by_id
            and _enum_for_field(
                capability_snapshot,
                fields_by_id[field_id],
            )
            is not None
        )
    }
    for field_id in field_ids:
        capability = fields_by_id.get(field_id)
        if capability is None:
            continue
        fields[field_id] = _field_fact(capability)
        enum = _enum_for_field(capability_snapshot, capability)
        if enum is None:
            continue
        if include_complete_enums and field_id in complete_enum_fields:
            codes = tuple(enum.values)
        else:
            codes = tuple(
                dict.fromkeys(
                    str(code)
                    for value in filters_by_field.get(field_id, ())
                    for code in _values(value)
                )
            )
        selected: dict[str, Mapping[str, Any]] = {}
        for code in codes:
            registered = enum.values.get(code)
            if registered is not None:
                selected[code] = {
                    "code": registered.code,
                    "label": registered.label,
                    "display": registered.display,
                }
            else:
                selected[code] = {
                    "code": code,
                    "label": code,
                    "display": code,
                }
        if selected:
            enum_values[field_id] = _freeze(selected)

    metrics: dict[str, Mapping[str, Any]] = {}
    metric_ids: set[str] = set()
    for metric_binding in contract.metrics:
        metric_id = metric_binding.metric_id
        metric = capability_snapshot.metrics.get(subject, {}).get(
            metric_id
        )
        metric_ids.add(metric_id)
        if metric is None:
            metrics[metric_id] = {
                "expression": metric_binding.expression,
                "label": metric_binding.label,
                "unit": metric_binding.unit,
                "source": metric_binding.source,
            }
            continue
        metrics[metric_id] = {
            "expression": metric.expression,
            "label": metric.label,
            "unit": metric.unit,
            "allowed_views": list(metric.allowed_views),
            "requires_join": list(metric.requires_join),
        }

    derived_metrics: dict[str, Mapping[str, Any]] = {}
    for metric_id, metric in capability_snapshot.derived_metrics.get(
        subject,
        {},
    ).items():
        if not set(metric.operands).issubset(metric_ids):
            continue
        derived_metrics[metric_id] = {
            "label": metric.label,
            "expression": metric.expression,
            "operands": list(metric.operands),
            "evaluator": metric.evaluator,
            "unit": metric.unit,
            "lower_is_better": metric.lower_is_better,
        }

    entity_families: dict[str, Mapping[str, Any]] = {}
    for item in contract.filters:
        resolver = str(item.resolver or "")
        if not resolver.startswith("entity_family:"):
            continue
        for family_id in resolver.removeprefix("entity_family:").split(","):
            family = capability_snapshot.entity_families.get(
                family_id.strip()
            )
            if family is None:
                continue
            entity_families[family.family_id] = {
                "entity": family.entity,
                "label": family.label,
                "aliases": list(family.aliases),
                "members": list(family.members),
                "resolution": dict(family.resolution),
            }

    assertions = _assertion_context(
        capability_snapshot.query_shapes,
        contract.required_assertions,
    )
    return CapabilityContext(
        subject=subject,
        fields=_freeze(fields),
        metrics=_freeze(metrics),
        derived_metrics=_freeze(derived_metrics),
        enum_values=_freeze(enum_values),
        entity_families=_freeze(entity_families),
        assertions=_freeze(assertions),
    )


def _requested_field_ids(contract: MdlQueryContract) -> tuple[str, ...]:
    values = [
        *(item.field_id for item in contract.filters),
        *contract.dimensions,
        *(item.field_id for item in contract.order_by),
        *(
            item.output_id
            for item in contract.outputs
            if item.kind == "field"
        ),
    ]
    metric_ids = {item.metric_id for item in contract.metrics}
    return tuple(
        dict.fromkeys(
            value
            for value in values
            if value and value not in metric_ids
        )
    )


def _enum_for_field(
    snapshot: CapabilitySnapshot,
    capability: Any,
) -> Any:
    if capability.enum_key and "." in capability.enum_key:
        table, column = capability.enum_key.split(".", 1)
        enum = snapshot.enums.get((table, column))
        if enum is not None:
            return enum
    return snapshot.enums.get(
        (capability.physical_table, capability.physical_column)
    )


def _field_fact(capability: Any) -> Mapping[str, Any]:
    return {
        "label": capability.label,
        "data_type": capability.data_type,
        "comment": capability.comment,
        "roles": list(capability.roles),
        "unit": capability.unit,
        "expression": capability.expression,
        "filter_expression": capability.filter_expression,
        "enum_key": capability.enum_key,
        "status": capability.status,
        "unavailable_reason": capability.unavailable_reason,
    }


def _values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


def _assertion_context(
    query_shapes: Mapping[str, Any],
    required: tuple[str, ...],
) -> dict[str, Mapping[str, Any]]:
    required_names = tuple(dict.fromkeys(str(item) for item in required))
    matched: dict[str, Mapping[str, Any]] = {
        name: {"source": "query_spec.required_assertions"}
        for name in required_names
    }

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            names: list[str] = []
            singular = value.get("required_assertion")
            if singular:
                names.append(str(singular))
            plural = value.get("required_assertions")
            if isinstance(plural, (list, tuple)):
                names.extend(str(item) for item in plural)
            assertion = value.get("assertion")
            if assertion:
                names.append(str(assertion))
            for name in names:
                if name in required_names:
                    matched[name] = _thaw(value)
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(query_shapes)
    return matched


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _thaw(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value
