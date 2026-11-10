from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MdlColumn:
    name: str
    data_type: str
    description: str
    is_calculated: bool = False
    expression: str = ""
    is_primary_key: bool = False


@dataclass(frozen=True)
class MdlModel:
    name: str
    physical_table: str
    description: str
    columns: Mapping[str, MdlColumn]


@dataclass(frozen=True)
class MdlCubeField:
    name: str
    expression: str
    data_type: str
    description: str


@dataclass(frozen=True)
class MdlCube:
    name: str
    base_object: str
    description: str
    measures: tuple[MdlCubeField, ...]
    dimensions: tuple[MdlCubeField, ...]


@dataclass(frozen=True)
class MdlRelationship:
    relationship_id: str
    models: tuple[str, str]
    join_type: str
    cardinality: str
    condition: str
    field_pairs: tuple[tuple[str, str], ...]
    source: str


@dataclass(frozen=True)
class MdlSnapshot:
    fingerprint: str
    models: Mapping[str, MdlModel]
    cubes: Mapping[str, MdlCube]
    relationships: Mapping[str, MdlRelationship]
    physical_table_to_model: Mapping[str, str] = field(default_factory=dict)
    source_dir: str = ""


@dataclass(frozen=True)
class RelationshipPath:
    models: tuple[str, ...]
    relationships: tuple[MdlRelationship, ...]


@dataclass(frozen=True)
class RelationshipSubgraph:
    root_model: str
    models: tuple[str, ...]
    relationships: tuple[MdlRelationship, ...]


@dataclass(frozen=True)
class MdlFieldBinding:
    field_id: str
    model: str
    column: str
    expression: str
    label: str
    data_type: str
    unit: str
    source: str
    roles: tuple[str, ...]
    contexts: tuple[str, ...]


@dataclass(frozen=True)
class MdlFilterBinding:
    field_id: str
    op: str
    value: Any
    display_value: str
    source: str
    resolver: str
    raw_text: str
    model: str
    column: str
    expression: str


@dataclass(frozen=True)
class MdlMetricBinding:
    metric_id: str
    expression: str
    label: str
    unit: str
    source: str
    requires_join: tuple[str, ...]


@dataclass(frozen=True)
class MdlOutputBinding:
    output_id: str
    kind: str
    expression: str
    alias: str
    label: str
    unit: str
    source: str


@dataclass(frozen=True)
class MdlOrderBinding:
    field_id: str
    expression: str
    direction: str
    source: str


@dataclass(frozen=True)
class MdlQueryContract:
    contract_id: str
    plan_id: str
    node_id: str
    query_spec_hash: str
    capability_snapshot_hash: str
    mdl_fingerprint: str
    contract_hash: str
    subject: str
    view: str
    root_model: str
    field_bindings: Mapping[str, MdlFieldBinding]
    filters: tuple[MdlFilterBinding, ...]
    dimensions: tuple[str, ...]
    metrics: tuple[MdlMetricBinding, ...]
    outputs: tuple[MdlOutputBinding, ...]
    group_by: tuple[str, ...]
    order_by: tuple[MdlOrderBinding, ...]
    limit: int | None
    required_assertions: tuple[str, ...]
    allowed_models: tuple[str, ...]
    required_relationship_ids: tuple[str, ...]
    intermediate_models: tuple[str, ...]
    mandatory_predicates: tuple[MdlFilterBinding, ...]
    output_aliases: Mapping[str, str]


@dataclass(frozen=True)
class CompilerFailure:
    code: str
    stage: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    contract_id: str = ""
    contract_hash: str = ""
    mdl_sql: str = ""
    physical_sql: str = ""


@dataclass(frozen=True)
class MdlSqlCandidate:
    source: str
    mdl_sql: str
    contract_id: str
    contract_hash: str
    used_models: tuple[str, ...] = ()
    used_relationship_ids: tuple[str, ...] = ()
    failure: CompilerFailure | None = None


@dataclass(frozen=True)
class DryPlanResult:
    accepted: bool
    mdl_sql: str
    physical_sql: str = ""
    error: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    contract_hash: str = ""
