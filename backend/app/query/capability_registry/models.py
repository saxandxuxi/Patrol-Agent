from __future__ import annotations

from dataclasses import dataclass, field
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
    roles: tuple[str, ...] = ()
    unit: str = ""
    physical_table: str = ""
    physical_column: str = ""
    requires_join: tuple[str, ...] = ()
    enum_key: str = ""


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
