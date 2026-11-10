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
    between_inclusive: bool = False


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
class CapabilityContext:
    subject: str
    fields: Mapping[str, Mapping[str, Any]]
    metrics: Mapping[str, Mapping[str, Any]]
    derived_metrics: Mapping[str, Mapping[str, Any]]
    enum_values: Mapping[str, Mapping[str, Mapping[str, Any]]]
    entity_families: Mapping[str, Mapping[str, Any]]
    assertions: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "fields": _plain(self.fields),
            "metrics": _plain(self.metrics),
            "derived_metrics": _plain(self.derived_metrics),
            "enum_values": _plain(self.enum_values),
            "entity_families": _plain(self.entity_families),
            "assertions": _plain(self.assertions),
        }


@dataclass(frozen=True)
class MdlContext:
    root_model: str
    models: Mapping[str, Mapping[str, Any]]
    relationships: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_model": self.root_model,
            "models": _plain(self.models),
            "relationships": _plain(self.relationships),
        }


@dataclass(frozen=True)
class PromptContextBundle:
    schema_version: int
    contract: MdlQueryContract
    business_capability: CapabilityContext
    mdl_subset: MdlContext

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract": self.contract,
            "business_capability": self.business_capability.to_dict(),
            "mdl_subset": self.mdl_subset.to_dict(),
            "relationship_subgraph": list(
                self.mdl_subset.relationships
            ),
        }


@dataclass(frozen=True)
class SqlIssue:
    code: str
    message: str
    path: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedMdlSql:
    sql: str
    dialect: str
    valid: bool
    root_query_type: str = ""
    table_aliases: Mapping[str, str] = field(default_factory=dict)
    qualified_columns: tuple[str, ...] = ()
    joins: tuple[str, ...] = ()
    projections: tuple[str, ...] = ()
    group_expressions: tuple[str, ...] = ()
    order_expressions: tuple[str, ...] = ()
    limit: int | None = None
    normalized_sql: str = ""
    issues: tuple[SqlIssue, ...] = ()


@dataclass(frozen=True)
class SqlAuditReport:
    accepted: bool
    issues: tuple[SqlIssue, ...]
    root_model: str = ""
    used_models: tuple[str, ...] = ()
    used_fields: tuple[str, ...] = ()
    used_relationships: tuple[str, ...] = ()
    normalized_structure: Mapping[str, Any] = field(default_factory=dict)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


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


@dataclass(frozen=True)
class MdlSqlPipelineResult:
    contract: MdlQueryContract
    context: PromptContextBundle
    relationship_subgraph: RelationshipSubgraph
    candidate: MdlSqlCandidate
    audit: SqlAuditReport
    dry_plan: DryPlanResult
    physical_sql: str
    deterministic_failure: CompilerFailure | None = None
