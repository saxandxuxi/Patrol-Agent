from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..calculations.models import CalculationRequest


SUPPORTED_QUERY_VIEWS = (
    "count",
    "sum",
    "avg",
    "distribution",
    "list",
    "detail",
    "top",
    "top_records",
    "top_groups",
    "trend",
    "bucket_compare",
    "photo",
    "data",
)


class QuerySpecError(ValueError):
    """Raised when model output cannot become a trustworthy QuerySpec."""


class QuerySpecUnsupported(QuerySpecError):
    """Raised when the requested scope requires unavailable capability."""


class QuerySpecAmbiguity(QuerySpecError):
    """Raised when the question itself needs a missing business definition."""


@dataclass(frozen=True)
class UserDecision:
    token: str
    choices: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ValueBinding:
    field: str
    op: str
    storage_value: Any
    display_value: str
    source: str
    resolver: str
    raw_text: str = ""


@dataclass(frozen=True)
class InputBinding:
    from_node: str
    from_field: str
    to_field: str
    op: str = "in"


@dataclass(frozen=True)
class IntentContract:
    original_question: str
    kind: str
    intent_family: str
    final_goal: str
    unknown_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuerySpec:
    id: str
    kind: str
    subject: str
    view: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: tuple[ValueBinding, ...]
    output_projection: tuple[str, ...]
    order_by: tuple[Any, ...]
    limit: int | None
    required_assertions: tuple[str, ...]
    question: str = ""
    input_bindings: tuple[InputBinding, ...] = ()
    calculation: CalculationRequest | None = None


@dataclass(frozen=True)
class PlanNode:
    id: str
    spec: QuerySpec
    depends_on: tuple[str, ...] = ()
    node_type: str = "query_data"


@dataclass(frozen=True)
class QueryPlan:
    question: str
    nodes: tuple[PlanNode, ...]
    response_mode: str = "data"


@dataclass(frozen=True)
class FailureEvidence:
    accepted: bool
    stage: str
    failure_code: str
    node_id: str
    reason: str
    expected: str = ""
    actual: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    allowed_actions: tuple[str, ...] = ()
    attempt: int = 1
    max_repair_rounds: int = 2


@dataclass(frozen=True)
class ExecutionObservation:
    node_id: str
    ok: bool
    engine: str
    sql: str
    columns: tuple[str, ...] = ()
    row_count: int = 0
    preview_rows: tuple[dict[str, Any], ...] = ()
    result_rows: tuple[dict[str, Any], ...] = ()
    rows_fetched: int = 0
    truncated: bool = False
    total_count: int | None = None
    null_summary: dict[str, Any] = field(default_factory=dict)
    distinct_summary: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    runtime_error: str | None = None
    query_fingerprint: str = ""


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    node_id: str = ""
    stage: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationReport:
    status: str
    issues: tuple[VerificationIssue, ...] = ()
    allowed_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnswerPackage:
    text: str
    sections: tuple[dict[str, Any], ...] = ()
    review_gate: dict[str, Any] | None = None


def scope_hash(spec: QuerySpec) -> str:
    scope = {
        "subject": spec.subject,
        "view": spec.view,
        "metrics": list(spec.metrics),
        "dimensions": list(spec.dimensions),
        "filters": [
            {
                "field": item.field,
                "op": item.op,
                "storage_value": item.storage_value,
            }
            for item in spec.filters
        ],
        "order_by": list(spec.order_by),
        "limit": spec.limit,
        "required_assertions": list(spec.required_assertions),
        "input_bindings": [
            {
                "from_node": item.from_node,
                "from_field": item.from_field,
                "to_field": item.to_field,
                "op": item.op,
            }
            for item in spec.input_bindings
        ],
    }
    payload = json.dumps(
        scope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def immutable_scope_hash(spec: QuerySpec) -> str:
    scope = {
        "subject": spec.subject,
        "filters": [
            {
                "field": item.field,
                "op": item.op,
                "storage_value": item.storage_value,
            }
            for item in spec.filters
        ],
        "input_bindings": [
            {
                "from_node": item.from_node,
                "from_field": item.from_field,
                "to_field": item.to_field,
                "op": item.op,
            }
            for item in spec.input_bindings
        ],
    }
    payload = json.dumps(
        scope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_scope_hash(plan: QueryPlan) -> str:
    nodes = [
        {"id": node.id, "scope_hash": scope_hash(node.spec)}
        for node in plan.nodes
    ]
    payload = json.dumps(
        nodes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
