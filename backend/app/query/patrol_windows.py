from __future__ import annotations

from dataclasses import replace
from typing import Any

from .contracts import (
    ExecutionObservation,
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecError,
)
from .result_projection import result_field_map


_BASE_SCOPE_FIELDS = frozenset(
    {
        "collection_time",
        "create_time",
        "device_code",
        "equipment_id",
        "patrol_status",
        "route_name",
        "routes_id",
    }
)
_BATCH_AXIS_TIME_FIELDS = frozenset({"collection_time", "create_time"})


def _base_field(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _has_field(spec: QuerySpec, field: str) -> bool:
    return any(
        _base_field(item) == field
        for item in (
            *spec.dimensions,
            *spec.output_projection,
        )
    )


def _is_recent_target(node: PlanNode) -> bool:
    return (
        node.node_type == "query_data"
        and node.spec.subject != "patrol"
        and "recent_window" in node.spec.required_assertions
        and _has_field(node.spec, "patrol_number")
    )


def _is_batch_node(node: PlanNode) -> bool:
    return (
        node.node_type == "query_data"
        and node.spec.subject == "patrol"
        and "recent_window" in node.spec.required_assertions
        and _has_field(node.spec, "patrol_number")
        and bool(node.spec.order_by)
    )


def _shared_scope_filters(spec: QuerySpec, registry: Any) -> tuple[Any, ...]:
    patrol_fields = (
        registry.snapshot.fields.get("patrol", {})
        if registry is not None
        else {}
    )
    selected = []
    for item in spec.filters:
        field = _base_field(item.field)
        if field in patrol_fields or field in _BASE_SCOPE_FIELDS:
            selected.append(item)
    return tuple(selected)


def _unique_node_id(plan: QueryPlan, base: str) -> str:
    used = {node.id for node in plan.nodes}
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def _batch_spec(
    target: QuerySpec,
    *,
    node_id: str,
    limit: int,
    filters: tuple[Any, ...],
) -> QuerySpec:
    return QuerySpec(
        id=node_id,
        kind=target.kind,
        subject="patrol",
        view="list",
        metrics=(),
        dimensions=("patrol_number", "create_time"),
        filters=filters,
        output_projection=("patrol_number", "create_time"),
        order_by=({"field": "create_time", "direction": "desc"},),
        limit=limit,
        required_assertions=("preserve_user_scope", "recent_window"),
        question=f"按巡检时间倒序取最近 {limit} 个真实巡检批次",
    )


def _without_batch_axis_time_fields(spec: QuerySpec) -> QuerySpec:
    dimensions = tuple(
        field
        for field in spec.dimensions
        if _base_field(field) not in _BATCH_AXIS_TIME_FIELDS
    )
    output_projection = tuple(
        field
        for field in spec.output_projection
        if _base_field(field) not in _BATCH_AXIS_TIME_FIELDS
    )
    order_by = tuple(
        item
        for item in spec.order_by
        if _base_field(item.get("field")) not in _BATCH_AXIS_TIME_FIELDS
    )
    view = spec.view
    if view == "trend":
        if spec.metrics:
            view = "distribution" if dimensions else "count"
        else:
            view = "list"
    if (
        dimensions == spec.dimensions
        and output_projection == spec.output_projection
        and order_by == spec.order_by
        and view == spec.view
    ):
        return spec
    return replace(
        spec,
        view=view,
        dimensions=dimensions,
        output_projection=output_projection,
        order_by=order_by,
    )


def complete_recent_window_plan(
    plan: QueryPlan,
    registry: Any = None,
) -> QueryPlan:
    """Resolve "recent N inspections" from the patrol timeline.

    A disease distribution must consume a real patrol-batch axis.  It cannot
    use its own filtered disease rows to decide which inspections count as
    the most recent N inspections.
    """

    if not any(
        _is_recent_target(node) or _is_batch_node(node)
        for node in plan.nodes
    ):
        return plan

    normalized_batches = {
        node.id: replace(
            node,
            spec=_batch_spec(
                node.spec,
                node_id=node.id,
                limit=node.spec.limit,
                filters=_shared_scope_filters(node.spec, registry),
            ),
        )
        for node in plan.nodes
        if _is_batch_node(node)
    }
    nodes: list[PlanNode] = []
    batch_by_target: dict[str, str] = {}
    available_batches = [
        normalized_batches[node.id]
        for node in plan.nodes
        if node.id in normalized_batches
    ]
    for node in plan.nodes:
        node = normalized_batches.get(node.id, node)
        if not _is_recent_target(node):
            nodes.append(node)
            continue
        existing_batch = next(
            (
                batch
                for batch in available_batches
                if any(
                    binding.from_node == batch.id
                    and _base_field(binding.to_field) == "patrol_number"
                    for binding in node.spec.input_bindings
                )
            ),
            None,
        )
        if existing_batch is None:
            limit = node.spec.limit
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise QuerySpecError(
                    f"节点 {node.id} 声明了 recent_window，但没有给出正整数 limit"
                )
            batch_id = _unique_node_id(plan, "recent_patrol_batches")
            existing_batch = PlanNode(
                id=batch_id,
                spec=_batch_spec(
                    node.spec,
                    node_id=batch_id,
                    limit=limit,
                    filters=_shared_scope_filters(node.spec, registry),
                ),
                node_type="query_data",
            )
            available_batches.append(existing_batch)
            nodes.append(existing_batch)
        batch_by_target[node.id] = existing_batch.id
        already_bound = any(
            binding.from_node == existing_batch.id
            and _base_field(binding.to_field) == "patrol_number"
            for binding in node.spec.input_bindings
        )
        bindings = node.spec.input_bindings
        if not already_bound:
            bindings = (
                *bindings,
                InputBinding(
                    from_node=existing_batch.id,
                    from_field="patrol_number",
                    to_field="patrol_number",
                    op="in",
                ),
            )
        dependencies = tuple(
            dict.fromkeys((*node.depends_on, existing_batch.id))
        )
        assertions = tuple(
            dict.fromkeys(
                (
                    *node.spec.required_assertions,
                    "complete_recent_window",
                )
            )
        )
        nodes.append(
            replace(
                node,
                depends_on=dependencies,
                spec=_without_batch_axis_time_fields(
                    replace(
                        node.spec,
                        filters=tuple(
                            item
                            for item in node.spec.filters
                            if _base_field(item.field) != "patrol_number"
                        ),
                        order_by=(),
                        limit=None,
                        input_bindings=bindings,
                        required_assertions=assertions,
                    ),
                ),
            )
        )

    batch_ids = {batch.id for batch in available_batches}
    if batch_ids:
        completed_nodes: list[PlanNode] = []
        for node in nodes:
            if node.id in batch_ids:
                completed_nodes.append(node)
                continue
            batch_bindings = tuple(
                binding
                for binding in node.spec.input_bindings
                if binding.from_node in batch_ids
                and _base_field(binding.to_field) == "patrol_number"
            )
            if not batch_bindings:
                completed_nodes.append(node)
                continue
            dependencies = (
                tuple(
                    dict.fromkeys(
                        (
                            *node.depends_on,
                            *(binding.from_node for binding in batch_bindings),
                        )
                    )
                )
                if node.node_type in {"query_data", "post_calc"}
                else node.depends_on
            )
            assertions = tuple(
                dict.fromkeys(
                    (
                        *node.spec.required_assertions,
                        "complete_recent_window",
                    )
                )
            )
            completed_nodes.append(
                replace(
                    node,
                    depends_on=dependencies,
                    spec=replace(
                        node.spec,
                        required_assertions=assertions,
                    ),
                )
            )
        nodes = completed_nodes

    rewritten: list[PlanNode] = []
    for node in nodes:
        if (
            node.id in batch_by_target
            or not batch_by_target
        ):
            rewritten.append(node)
            continue
        batch_ids = {
            batch_by_target[binding.from_node]
            for binding in node.spec.input_bindings
            if binding.from_node in batch_by_target
            and _base_field(binding.from_field) == "patrol_number"
            and _base_field(binding.to_field) == "patrol_number"
        }
        calculation = node.spec.calculation
        calculation_changed = False
        if (
            calculation is not None
            and calculation.tool == "calculate_datetime_interval"
        ):
            operands = []
            for operand in calculation.operands:
                source_node = str(operand.source.get("node") or "")
                if source_node in batch_by_target:
                    batch_ids.add(batch_by_target[source_node])
                    operand = replace(
                        operand,
                        source={
                            **operand.source,
                            "node": batch_by_target[source_node],
                            "field": "create_time",
                        },
                    )
                    calculation_changed = True
                operands.append(operand)
            if calculation_changed:
                calculation = replace(
                    calculation,
                    operands=tuple(operands),
                )
        if not batch_ids and not calculation_changed:
            rewritten.append(node)
            continue
        bindings = tuple(
            replace(
                binding,
                from_node=batch_by_target[binding.from_node],
                from_field="patrol_number",
            )
            if (
                binding.from_node in batch_by_target
                and _base_field(binding.from_field) == "patrol_number"
                and _base_field(binding.to_field) == "patrol_number"
            )
            else binding
            for binding in node.spec.input_bindings
        )
        dependencies = (
            tuple(
                dict.fromkeys(
                    batch_by_target.get(dependency, dependency)
                    for dependency in node.depends_on
                )
            )
            if node.node_type in {"query_data", "post_calc"}
            else node.depends_on
        )
        rewritten.append(
            replace(
                node,
                spec=replace(
                    node.spec,
                    input_bindings=bindings,
                    calculation=calculation,
                ),
                depends_on=dependencies,
            )
        )

    consumed = {
        dependency
        for node in rewritten
        for dependency in node.depends_on
    }
    consumed.update(
        binding.from_node
        for node in rewritten
        for binding in node.spec.input_bindings
    )
    final_nodes = [
        node
        for node in rewritten
        if node.id not in consumed
    ]
    has_final_post_calc = any(
        node.node_type == "post_calc" for node in final_nodes
    )
    if has_final_post_calc:
        rewritten = [
            node
            for node in rewritten
            if not (
                node.id in batch_by_target
                and node.id not in consumed
            )
        ]

    return QueryPlan(
        question=plan.question,
        nodes=tuple(rewritten),
        response_mode=plan.response_mode,
    )


def _metric_zero(metric_id: str) -> int | None:
    normalized = str(metric_id).rsplit(".", 1)[-1].lower()
    if normalized == "count" or normalized.startswith(("total_", "sum_")):
        return 0
    return None


def complete_recent_window_groups(
    spec: QuerySpec,
    observation: ExecutionObservation,
    batch_spec: QuerySpec,
    batch_observation: ExecutionObservation,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Complete a batch series from the real patrol axis, using zero for counts."""

    if (
        "complete_recent_window" not in spec.required_assertions
        or not _has_field(spec, "patrol_number")
        or not batch_observation.result_rows
        and not batch_observation.preview_rows
    ):
        return list(observation.result_rows or observation.preview_rows), []

    rows = [
        dict(row)
        for row in (observation.result_rows or observation.preview_rows)
    ]
    columns = list(observation.columns)
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    target_fields = result_field_map(spec, columns)
    target_column = target_fields.get("patrol_number")
    batch_columns = list(batch_observation.columns)
    batch_fields = result_field_map(batch_spec, batch_columns)
    batch_column = batch_fields.get("patrol_number")
    if not target_column or not batch_column:
        return rows, []

    rows_by_batch = {
        _base_field(row.get(target_column)): row
        for row in rows
        if _base_field(row.get(target_column))
    }
    metric_columns = result_field_map(spec, columns)
    ordered: list[dict[str, Any]] = []
    filled: list[str] = []
    used: set[str] = set()
    batch_rows = (
        batch_observation.result_rows or batch_observation.preview_rows
    )
    for batch_row in batch_rows:
        batch_id = _base_field(batch_row.get(batch_column))
        if not batch_id or batch_id in used:
            continue
        used.add(batch_id)
        row = rows_by_batch.get(batch_id)
        if row is None:
            row = {column: None for column in columns}
            row[target_column] = batch_id
            for metric_id in spec.metrics:
                column = metric_columns.get(
                    str(metric_id).rsplit(".", 1)[-1]
                )
                if column:
                    row[column] = _metric_zero(str(metric_id))
            filled.append(batch_id)
        ordered.append(dict(row))
    ordered.extend(
        dict(row)
        for row in rows
        if _base_field(row.get(target_column))
        and _base_field(row.get(target_column)) not in used
    )
    return ordered, filled
