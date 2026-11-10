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
