from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, is_dataclass, replace
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class SubjectCapability:
    subject_id: str
    base_table: str
    joins: dict[str, dict[str, str]] = field(default_factory=dict)
    allowed_views: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldCapability:
    field_id: str
    expression: str
    label: str
    data_type: str = ""
    comment: str = ""
    roles: tuple[str, ...] = ()
    unit: str = ""
    physical_table: str = ""
    physical_column: str = ""
    requires_join: tuple[str, ...] = ()
    enum_key: str = ""
    status: str = "available"
    unavailable_reason: str = ""


@dataclass(frozen=True)
class MetricCapability:
    metric_id: str
    expression: str
    label: str
    unit: str = ""
    allowed_views: tuple[str, ...] = ()
    requires_join: tuple[str, ...] = ()


@dataclass(frozen=True)
class DerivedMetricCapability:
    metric_id: str
    label: str
    expression: str
    operands: tuple[str, ...]
    evaluator: str = "post_calc"
    unit: str = ""
    lower_is_better: bool = False


@dataclass(frozen=True)
class EnumValueCapability:
    code: str
    label: str
    display: str


@dataclass(frozen=True)
class EnumCapability:
    key: str
    table: str
    column: str
    label: str
    values: dict[str, EnumValueCapability]


@dataclass(frozen=True)
class EntityCapability:
    entity_id: str
    source: dict[str, Any]
    identity: str


@dataclass(frozen=True)
class EntityFamilyCapability:
    family_id: str
    entity: str
    label: str
    aliases: tuple[str, ...]
    members: tuple[str, ...]
    resolution: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilitySnapshot:
    version: str
    config_hash: str
    subjects: dict[str, SubjectCapability]
    fields: dict[str, dict[str, FieldCapability]]
    metrics: dict[str, dict[str, MetricCapability]]
    derived_metrics: dict[str, dict[str, DerivedMetricCapability]]
    enums: dict[tuple[str, str], EnumCapability]
    entities: dict[str, EntityCapability]
    entity_families: dict[str, EntityFamilyCapability]
    aliases: dict[str, Any]
    identifiers: dict[str, Any]
    query_shapes: dict[str, Any]
    dimensions: dict[str, Any]
    predicates: dict[str, Any]
    time_buckets: dict[str, Any]
    result_words: dict[str, Any]
    repair_policy: dict[str, Any]
    templates: dict[str, Any]
    calculators: dict[str, Any]


@dataclass(frozen=True)
class PlannerFieldView:
    field_id: str
    label: str
    data_type: str
    comment: str
    roles: tuple[str, ...]
    enum_values: tuple[EnumValueCapability, ...] = ()
    status: str = "available"
    unavailable_reason: str = ""


@dataclass(frozen=True)
class PlannerCapabilityView:
    subject: str
    base_table: str
    fields: dict[str, PlannerFieldView]
    metrics: tuple[str, ...]
    derived_metrics: tuple[str, ...]
    allowed_views: tuple[str, ...]
    reach_evidence: tuple[dict[str, Any], ...] = ()
    metric_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    derived_metric_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    calculators: dict[str, dict[str, Any]] = field(default_factory=dict)
    business_values: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "base_table": self.base_table,
            "metrics": self.metric_details,
            "derived_metrics": self.derived_metric_details,
            "calculators": self.calculators,
            "business_values": self.business_values,
            "allowed_views": list(self.allowed_views),
            "fields": {
                field_id: {
                    "label": field.label,
                    "type": field.data_type,
                    "comment": field.comment,
                    "roles": list(field.roles),
                    "status": field.status,
                    "unavailable_reason": field.unavailable_reason,
                    "enum_values": [
                        {
                            "code": item.code,
                            "label": item.label,
                            "display": item.display,
                        }
                        for item in field.enum_values
                    ],
                }
                for field_id, field in self.fields.items()
            },
            "field_reach_evidence": list(self.reach_evidence),
        }


def freeze_snapshot(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: freeze_snapshot(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_snapshot(item) for item in value)
    if is_dataclass(value):
        return replace(
            value,
            **{
                item.name: freeze_snapshot(getattr(value, item.name))
                for item in value.__dataclass_fields__.values()
            },
        )
    return value
