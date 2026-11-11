from __future__ import annotations

from dataclasses import replace

from .contracts import PlanNode, QueryPlan, QuerySpec, QuerySpecError


def _single_route(spec: QuerySpec) -> bool:
    values: set[str] = set()
    for binding in spec.filters:
        field = str(binding.field).rsplit(".", 1)[-1]
        if field not in {"route_name", "routes_id"}:
            continue
        raw = binding.storage_value
        items = raw if isinstance(raw, (list, tuple, set)) else (raw,)
        values.update(str(item) for item in items if str(item).strip())
    return len(values) == 1


def complete_single_route_trend_context(plan: QueryPlan) -> QueryPlan:
    """Carry the route-size fact into a single-route trend result."""

    trends = [
        node
        for node in plan.nodes
        if node.node_type == "query_data"
        and node.spec.subject == "disease"
        and node.spec.view == "trend"
        and _single_route(node.spec)
    ]
    if not trends:
        return plan
    scope = trends[0].spec
    context = next(
        (
            node
            for node in plan.nodes
            if node.node_type == "query_data"
            and node.spec.subject == "disease"
            and node.spec.metrics == ("route_length_km",)
            and _same_route_scope(node.spec, scope)
        ),
        None,
    )
    nodes = [
        (
            replace(
                node,
                spec=replace(
                    node.spec,
                    metrics=tuple(
                        metric
                        for metric in node.spec.metrics
                        if metric != "route_length_km"
                    ),
                    output_projection=tuple(
                        field
                        for field in node.spec.output_projection
                        if field != "route_length_km"
                    ),
                ),
            )
            if node in trends and "route_length_km" in node.spec.metrics
            else node
        )
        for node in plan.nodes
    ]
    if context is None:
        used_ids = {node.id for node in nodes}
        context_id = "q_route_length"
        index = 2
        while context_id in used_ids:
            context_id = f"q_route_length_{index}"
            index += 1
        context = PlanNode(
            id=context_id,
            spec=QuerySpec(
                id=context_id,
                kind=scope.kind,
                subject=scope.subject,
                view="distribution",
                metrics=("route_length_km",),
                dimensions=("route_name", "routes_id"),
                filters=tuple(
                    binding
                    for binding in scope.filters
                    if str(binding.field).rsplit(".", 1)[-1]
                    in {"route_name", "routes_id"}
                ),
                output_projection=(
                    "route_name",
                    "routes_id",
                    "route_length_km",
                ),
                order_by=(),
                limit=None,
                required_assertions=(
                    "preserve_user_scope",
                    "analysis_context",
                ),
                question="读取当前路线台账长度，作为趋势解释上下文",
            ),
            node_type="query_data",
        )
        insert_at = next(
            (
                index
                for index, node in enumerate(nodes)
                if node.node_type == "post_calc"
            ),
            len(nodes),
        )
        nodes.insert(insert_at, context)
    trend_ids = {node.id for node in trends}
    context_id = context.id
    nodes = [
        (
            replace(
                node,
                depends_on=tuple(
                    dict.fromkeys((*node.depends_on, context_id))
                ),
            )
            if node.node_type == "post_calc"
            and "analysis_only" in node.spec.required_assertions
            and any(dependency in trend_ids for dependency in node.depends_on)
            else node
        )
        for node in nodes
    ]
    return replace(plan, nodes=tuple(nodes))


def _same_route_scope(left: QuerySpec, right: QuerySpec) -> bool:
    def route_values(spec: QuerySpec) -> set[tuple[str, str]]:
        values = set()
        for binding in spec.filters:
            field = str(binding.field).rsplit(".", 1)[-1]
            if field not in {"route_name", "routes_id"}:
                continue
            raw = binding.storage_value
            items = raw if isinstance(raw, (list, tuple, set)) else (raw,)
            values.update(
                (field, str(item))
                for item in items
                if str(item).strip()
            )
        return values

    return bool(route_values(left)) and route_values(left) == route_values(right)


def complete_route_metric_identity(plan: QueryPlan) -> QueryPlan:
    """Prove route.id grain whenever a grouped disease query reads route size."""

    nodes = []
    for node in plan.nodes:
        spec = node.spec
        if (
            node.node_type == "query_data"
            and spec.subject == "disease"
            and "route_length_km" in spec.metrics
            and spec.dimensions
            and "routes_id" not in spec.dimensions
        ):
            spec = replace(
                spec,
                dimensions=(*spec.dimensions, "routes_id"),
                output_projection=(
                    *spec.output_projection,
                    "routes_id",
                ),
            )
        nodes.append(node if spec == node.spec else replace(node, spec=spec))
    return replace(plan, nodes=tuple(nodes))


def complete_route_analysis_context(plan: QueryPlan) -> QueryPlan:
    """Give route-level multi-metric analyses the route-size denominator."""

    nodes_by_id = {node.id: node for node in plan.nodes}
    additions: dict[str, tuple[str, ...]] = {}
    assertion_additions: dict[str, tuple[str, ...]] = {}
    for node in plan.nodes:
        if node.node_type != "post_calc" or not node.depends_on:
            continue
        grouped = [
            nodes_by_id[dependency]
            for dependency in node.depends_on
            if dependency in nodes_by_id
            and nodes_by_id[dependency].node_type == "query_data"
            and nodes_by_id[dependency].spec.subject == "disease"
            and "route_name"
            in {
                str(field).rsplit(".", 1)[-1]
                for field in nodes_by_id[dependency].spec.dimensions
            }
            and len(nodes_by_id[dependency].spec.metrics) >= 2
        ]
        if not grouped:
            continue
        for dependency in grouped:
            spec = dependency.spec
            if "route_length_km" not in spec.metrics:
                additions[dependency.id] = (
                    *spec.metrics,
                    "route_length_km",
                )
            assertion_additions[node.id] = (
                *node.spec.required_assertions,
                "multi_metric_comparison",
            )
    if not additions and not assertion_additions:
        return plan
    nodes = []
    for node in plan.nodes:
        spec = node.spec
        if node.id in additions:
            spec = replace(
                spec,
                metrics=additions[node.id],
                output_projection=(
                    *spec.output_projection,
                    *(
                        ("route_length_km",)
                        if "route_length_km" not in spec.output_projection
                        else ()
                    ),
                ),
            )
        if node.id in assertion_additions:
            spec = replace(
                spec,
                required_assertions=tuple(
                    dict.fromkeys(assertion_additions[node.id])
                ),
            )
        nodes.append(node if spec == node.spec else replace(node, spec=spec))
    return complete_route_metric_identity(
        replace(plan, nodes=tuple(nodes))
    )


def _comparison_candidate_issue(plan: QueryPlan) -> str:
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        if (
            node.node_type != "post_calc"
            or "multi_metric_comparison"
            not in node.spec.required_assertions
        ):
            continue
        for dependency in node.depends_on:
            source = nodes_by_id.get(dependency)
            if (
                source is not None
                and source.node_type == "query_data"
                and source.spec.dimensions
                and source.spec.metrics
                and source.spec.limit is not None
                and source.spec.limit <= 1
            ):
                return (
                    f"分析节点 {node.id} 要对多个候选做综合比较，"
                    f"但数据节点 {source.id} 只保留 limit=1 个候选；"
                    "请保留用户范围并移除这个截断，或明确说明只比较该对象。"
                )
    return ""


def _inherit_single_dimension_comparison(plan: QueryPlan) -> QueryPlan:
    nodes_by_id = {node.id: node for node in plan.nodes}
    changed = False
    nodes = []
    for node in plan.nodes:
        if (
            node.node_type != "post_calc"
            or "analysis_only" not in node.spec.required_assertions
            or "multi_metric_comparison" in node.spec.required_assertions
        ):
            nodes.append(node)
            continue
        dependencies = [
            nodes_by_id[dependency]
            for dependency in node.depends_on
            if dependency in nodes_by_id
        ]
        if not (
            len(dependencies) == 1
            and dependencies[0].node_type == "query_data"
            and dependencies[0].spec.view == "distribution"
            and len(dependencies[0].spec.dimensions) == 1
            and len(dependencies[0].spec.metrics) >= 2
        ):
            nodes.append(node)
            continue
        changed = True
        nodes.append(
            replace(
                node,
                spec=replace(
                    node.spec,
                    required_assertions=(
                        *node.spec.required_assertions,
                        "multi_metric_comparison",
                    ),
                ),
            )
        )
    return replace(plan, nodes=tuple(nodes)) if changed else plan


def complete_analysis_answer(plan: QueryPlan, intent: str) -> QueryPlan:
    """Ensure an independently classified analysis question has a final answer node."""

    plan = complete_single_route_trend_context(plan)
    plan = complete_route_metric_identity(plan)
    if str(intent or "").strip() != "analysis":
        return plan
    data_nodes = [
        node for node in plan.nodes if node.node_type == "query_data"
    ]
    if not data_nodes:
        return plan
    has_analysis = any(
        node.node_type == "post_calc"
        and "analysis_only" in node.spec.required_assertions
        and any(
            dependency in {item.id for item in data_nodes}
            for dependency in node.depends_on
        )
        for node in plan.nodes
    )
    if has_analysis:
        completed = _inherit_single_dimension_comparison(
            complete_route_analysis_context(
                replace(plan, response_mode="analysis")
            )
        )
        issue = _comparison_candidate_issue(completed)
        if issue:
            raise QuerySpecError(issue)
        return completed
    subjects = {node.spec.subject for node in data_nodes}
    if len(subjects) != 1:
        return plan
    used_ids = {node.id for node in plan.nodes}
    node_id = "pc_analysis"
    index = 2
    while node_id in used_ids:
        node_id = f"pc_analysis_{index}"
        index += 1
    first = data_nodes[0].spec
    summary_assertions = [
        "analysis_only",
        "preserve_user_scope",
    ]
    summary = QuerySpec(
        id=node_id,
        kind=first.kind,
        subject=first.subject,
        view="data",
        metrics=(),
        dimensions=(),
        filters=(),
        output_projection=(),
        order_by=(),
        limit=None,
        required_assertions=tuple(summary_assertions),
        question=(
            "基于依赖节点的真实结果回答问题并给出结论；"
            "只能复述已有数值，不产生新数字。"
        ),
    )
    completed = _inherit_single_dimension_comparison(
        complete_route_analysis_context(
            QueryPlan(
                question=plan.question,
                nodes=(
                    *plan.nodes,
                    PlanNode(
                        id=node_id,
                        spec=summary,
                        depends_on=tuple(node.id for node in data_nodes),
                        node_type="post_calc",
                    ),
                ),
                response_mode="analysis",
            ),
        )
    )
    issue = _comparison_candidate_issue(completed)
    if issue:
        raise QuerySpecError(issue)
    return completed
