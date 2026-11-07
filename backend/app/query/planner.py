from __future__ import annotations

from .contracts import IntentContract, PlanNode, QueryPlan, QuerySpec, QuerySpecError
from .fields import FieldReachSet, PhysicalSchemaView
from .validator import validate_query_spec
from .fields import OutputProjection


def build_query_plan(
    contract: IntentContract,
    specs: tuple[QuerySpec, ...],
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
    dependencies: dict[str, tuple[str, ...]] | None = None,
    node_types: dict[str, str] | None = None,
    response_mode: str = "data",
) -> QueryPlan:
    if not specs:
        raise QuerySpecError("模型没有产出任何查询视图")
    seen: set[str] = set()
    nodes: list[PlanNode] = []
    deps_by_node = dependencies or {}
    types_by_node = node_types or {}
    for spec in specs:
        if spec.id in seen:
            raise QuerySpecError(f"查询节点 id 重复：{spec.id}")
        seen.add(spec.id)
        issues = validate_query_spec(
            spec,
            schema,
            OutputProjection(fields=()),
            node_type=types_by_node.get(spec.id, "query_data"),
        )
        if issues:
            raise QuerySpecError("；".join(item.message for item in issues))
        nodes.append(
            PlanNode(
                id=spec.id,
                spec=spec,
                depends_on=tuple(deps_by_node.get(spec.id, ())),
                node_type=types_by_node.get(spec.id, "query_data"),
            )
        )
    return QueryPlan(
        question=contract.original_question,
        nodes=tuple(nodes),
        response_mode=response_mode,
    )
