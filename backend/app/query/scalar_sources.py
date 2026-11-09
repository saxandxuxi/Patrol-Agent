from __future__ import annotations

from dataclasses import replace
from typing import Any

from .contracts import PlanNode, QueryPlan, QuerySpec


_GROUPED_VIEWS = frozenset(
    {"distribution", "trend", "top_groups", "bucket_compare"}
)


def _base_field(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _single_value_bindings(spec: QuerySpec) -> set[str]:
    fields: set[str] = set()
    for binding in spec.filters:
        if binding.op not in {"=", "in"}:
            continue
        values = (
            list(binding.storage_value)
            if isinstance(binding.storage_value, (list, tuple))
            else [binding.storage_value]
        )
        if len(values) == 1 and values[0] not in (None, ""):
            fields.add(_base_field(binding.field))
    return fields


def _has_deterministic_single_row(spec: QuerySpec) -> bool:
    dimensions = {_base_field(item) for item in spec.dimensions}
    return bool(dimensions) and dimensions.issubset(
        _single_value_bindings(spec)
    )


def _with_group_order(spec: QuerySpec) -> QuerySpec:
    if spec.view not in _GROUPED_VIEWS or not spec.dimensions:
        return spec
    present = {
        _base_field(item.get("field"))
        for item in spec.order_by
        if isinstance(item, dict)
    }
    additions = tuple(
        {"field": _base_field(field), "direction": "asc"}
        for field in spec.dimensions
        if _base_field(field) not in present
    )
    if not additions:
        return spec
    return replace(spec, order_by=(*spec.order_by, *additions))


def complete_scalar_source_selections(plan: QueryPlan) -> QueryPlan:
    """Make scalar row selection deterministic for grouped upstream results."""

    by_id = {node.id: node for node in plan.nodes}
    updated_specs: dict[str, QuerySpec] = {}
    updated_nodes: dict[str, PlanNode] = {}

    for node in plan.nodes:
        calculation = node.spec.calculation
        if (
            node.node_type != "post_calc"
            or calculation is None
            or calculation.tool != "calculate_scalar"
        ):
            continue
        changed = False
        operands = []
        for operand in calculation.operands:
            source_node = by_id.get(str(operand.source.get("node") or ""))
            if source_node is None or not operand.source:
                operands.append(operand)
                continue
            source_spec = updated_specs.get(
                source_node.id,
                source_node.spec,
            )
            if source_spec.view not in _GROUPED_VIEWS:
                operands.append(operand)
                continue
            row_index = operand.source.get("row_index")
            if row_index is None:
                if not _has_deterministic_single_row(source_spec):
                    operands.append(operand)
                    continue
                row_index = 0
                changed = True
            if not isinstance(row_index, int) or isinstance(row_index, bool):
                operands.append(operand)
                continue
            ordered_spec = _with_group_order(source_spec)
            if ordered_spec != source_spec:
                updated_specs[source_node.id] = ordered_spec
                updated_nodes[source_node.id] = replace(
                    source_node,
                    spec=ordered_spec,
                )
            operands.append(
                replace(
                    operand,
                    source={**operand.source, "row_index": row_index},
                )
            )
        if changed or tuple(operands) != calculation.operands:
            updated_nodes[node.id] = replace(
                node,
                spec=replace(
                    node.spec,
                    calculation=replace(
                        calculation,
                        operands=tuple(operands),
                    ),
                ),
            )
    if not updated_nodes:
        return plan
    return replace(
        plan,
        nodes=tuple(updated_nodes.get(node.id, node) for node in plan.nodes),
    )
