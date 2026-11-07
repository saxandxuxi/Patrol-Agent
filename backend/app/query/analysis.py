from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..services import prompts
from ..services.llm import LLMError, chat_json, role_model
from ..calculations.models import (
    CalculationOperand,
    CalculationOutput,
    CalculationRequest,
)
from ..calculations.operations import roles_for_tool
from ..semantic_aids import canonical_field, detect_evidence
from .audit import audit_query_plan
from .capability_registry import CapabilityIssue, CapabilityRegistry
from .contracts import (
    InputBinding,
    IntentContract,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecAmbiguity,
    QuerySpecError,
    QuerySpecUnsupported,
    ValueBinding,
)
from .fields import FieldReachSet, OutputProjection, PhysicalSchemaView
from .periods import normalize_named_period_window
from .planner import build_query_plan
from .shape_contracts import canonical_view, normalize_top_view
from .spec_mdl import UnsupportedQuerySpec, compile_spec_mdl
from .validator import validate_query_spec
from .values import resolve_value


def _text_context(value: Any, limit: int = 8000) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:limit]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _metric_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            metric = item.strip()
        elif isinstance(item, dict):
            aggregate = str(item.get("agg") or item.get("aggregation") or "").lower()
            field = str(item.get("field") or item.get("column") or "").strip()
            if aggregate == "count":
                metric = "count"
            elif aggregate in {"avg", "average"} and field:
                metric = f"average_{field}"
            elif aggregate == "sum" and field:
                metric = f"sum_{field}"
            else:
                metric = field
        else:
            metric = ""
        if "(" in metric and metric.endswith(")"):
            aggregate, _, field = metric.partition("(")
            aggregate = aggregate.strip().lower()
            field = field[:-1].strip().lower()
            if aggregate == "count":
                metric = "count"
            elif aggregate == "sum":
                metric = {
                    "area": "sum_area",
                    "disease_length": "sum_disease_length",
                    "effective_disease_length": "sum_effective_disease_length",
                }.get(field, f"sum_{field}")
            elif aggregate in {"avg", "average"}:
                metric = {
                    "area": "average_area",
                    "disease_length": "average_length",
                    "effective_disease_length": "average_length",
                }.get(field, f"average_{field}")
        metric = _METRIC_ALIASES.get(metric, metric)
        if metric and metric not in result:
            result.append(metric)
    return tuple(result)


_METRIC_ALIASES = {
    "sum_length": "sum_disease_length",
    "total_length": "sum_disease_length",
    "avg_length": "average_length",
    "sum_effective_length": "sum_effective_disease_length",
}


def _canonical_view(
    value: Any,
    metrics: tuple[str, ...] = (),
    dimensions: tuple[str, ...] = (),
    node_type: str = "query_data",
) -> str:
    return canonical_view(value, metrics, dimensions, node_type)


def _order_items(value: Any) -> tuple[Any, ...]:
    result: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        if normalized.get("field"):
            normalized["field"] = canonical_field(str(normalized["field"]))
        if "direction" not in normalized:
            order = normalized.get("order")
            if order:
                normalized["direction"] = str(order)
        result.append(normalized)
    return tuple(result)


def _canonical_dimensions(subject: str, dimensions: tuple[str, ...]) -> tuple[str, ...]:
    normalized_dimensions = []
    seen: set[str] = set()
    for item in dimensions:
        compact = item.replace(" ", "").lower()
        if compact.startswith("date_format(") and any(
            field in compact
            for field in ("create_time", "collection_time", "collection_month", "create_month")
        ):
            item = "month"
        item = canonical_field(item)
        normalized = (
            "equipment_id"
            if subject == "patrol"
            and item in {"device_code", "device_id", "device", "equipment"}
            else "route_name"
            if subject == "disease" and item == "routes_id"
            else item
        )
        if normalized not in seen:
            seen.add(normalized)
            normalized_dimensions.append(normalized)
    return tuple(normalized_dimensions)


def _canonicalize_input_bindings(specs: list[QuerySpec]) -> None:
    by_id = {spec.id: spec for spec in specs}
    for index, spec in enumerate(specs):
        bindings = []
        for item in spec.input_bindings:
            from_field = str(item.from_field).rsplit(".", 1)[-1]
            to_field = str(item.to_field).rsplit(".", 1)[-1]
            source = by_id.get(item.from_node)
            source_fields = {
                str(field).rsplit(".", 1)[-1]
                for field in (
                    (*source.dimensions, *source.metrics, *source.output_projection)
                    if source is not None
                    else ()
                )
            }
            target_fields = {
                str(field).rsplit(".", 1)[-1]
                for field in (
                    *spec.dimensions,
                    *spec.filters,
                    *spec.metrics,
                    *spec.output_projection,
                )
                if not isinstance(field, ValueBinding)
            }
            if (
                from_field == "routes_id"
                and "route_name" in source_fields
            ):
                from_field = "route_name"
            if (
                to_field == "routes_id"
                and "route_name" in target_fields
            ):
                to_field = "route_name"
            bindings.append(
                InputBinding(
                    from_node=item.from_node,
                    from_field=from_field,
                    to_field=to_field,
                    op=item.op,
                )
            )
        specs[index] = QuerySpec(
            **{**spec.__dict__, "input_bindings": tuple(bindings)}
        )


def _plan_subject_issue(plan: QueryPlan, subject: str) -> str:
    if not subject:
        return ""
    nodes_by_id = {node.id: node for node in plan.nodes}
    consumed = {
        dependency
        for node in plan.nodes
        for dependency in node.depends_on
    }
    consumed.update(
        binding.from_node
        for node in plan.nodes
        for binding in node.spec.input_bindings
    )
    for node in plan.nodes:
        if node.id in consumed:
            continue
        effective_subject = node.spec.subject
        if node.node_type == "post_calc":
            dependency_subjects = {
                nodes_by_id[dependency].spec.subject
                for dependency in node.depends_on
                if dependency in nodes_by_id
            }
            if len(dependency_subjects) == 1:
                effective_subject = next(iter(dependency_subjects))
        if effective_subject != subject:
            return (
                f"最终回答主体 {effective_subject} 与已确定主体 {subject} 不一致"
            )
    return ""


def _plan_intent_issue(plan: QueryPlan, intent_family: str) -> str:
    if intent_family != "trend":
        return ""
    trend_nodes = [
        node
        for node in plan.nodes
        if node.node_type == "post_calc" and node.spec.view == "trend"
    ]
    if trend_nodes:
        return ""
    return "趋势问题必须增加一个消费数据结果的 post_calc 节点，并使用 view=trend"


def _plan_shaping_issue(plan: QueryPlan, intent_family: str) -> str:
    for node in plan.nodes:
        if (
            node.node_type == "query_data"
            and node.spec.view == "list"
            and node.spec.limit is not None
            and not node.spec.order_by
            and (
                intent_family == "trend"
                or "recent_window" in node.spec.required_assertions
            )
        ):
            return (
                f"节点 {node.id} 使用 limit={node.spec.limit} 时必须声明 order_by，"
                "禁止无排序截取列表"
            )
    return ""


def _spec_order_issue(spec: QuerySpec, registry: CapabilityRegistry | None) -> str:
    if registry is None:
        return ""
    allowed = {
        *registry.snapshot.fields.get(spec.subject, {}),
        *registry.snapshot.metrics.get(spec.subject, {}),
        *registry.snapshot.derived_metrics.get(spec.subject, {}),
        *spec.dimensions,
        *spec.output_projection,
    }
    for item in spec.order_by:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        if field and field not in allowed:
            return (
                f"节点 {spec.id} 的排序字段 {field} 不属于主体 {spec.subject}；"
                f"可用字段：{'、'.join(sorted(allowed))}"
            )
    return ""


def _spec_extreme_order_issue(
    spec: QuerySpec,
    node_types: dict[str, str],
) -> str:
    """极值问法 + 无排序 = 返回任意记录，必须在执行前拦住。

    实测问题：“全库面积最大的病害在哪条路？”被规划成不带排序的明细查询，
    结果给了一条面积 0.46 的记录。这不是数据问题，是契约缺失：带条数上限
    的明细查询没有排序字段时，任何一行都可能被返回。
    """
    if node_types.get(spec.id) != "query_data":
        return ""
    if spec.view not in {"list", "detail", "data", "top_records"}:
        return ""
    if spec.order_by or spec.limit is None:
        return ""
    return (
        f"节点 {spec.id} 的问题是极值口径（最大/最多/最高/排名），"
        "但计划里没有排序字段：现在这条 SQL 返回的是任意几条记录，不是极值。"
        "请补 order_by（极值对应的指标或字段及其方向）后重试。"
    )


def _unavailable_assist_reason(
    evidence: list[dict[str, Any]],
    registry: CapabilityRegistry | None,
) -> str:
    """代码规则命中的字段已声明不可用时，给出具体的能力缺失原因。"""
    if registry is None:
        return ""
    for item in evidence:
        if not isinstance(item, dict):
            continue
        field_id = canonical_field(str(item.get("candidate_field") or ""))
        if not field_id:
            continue
        for fields in registry.snapshot.fields.values():
            capability = fields.get(field_id)
            if capability is None or capability.status not in {
                "unavailable",
                "lookup_required",
            }:
                continue
            return "当前数据能力不支持：" + (
                capability.unavailable_reason or "字段能力不可用"
            )
    return ""


def _assist_field_issue(
    specs: list[QuerySpec],
    evidence: list[dict[str, Any]],
    registry: CapabilityRegistry | None,
    node_types: dict[str, str],
) -> str:
    query_specs = [
        spec
        for spec in specs
        if node_types.get(spec.id) != "post_calc"
    ]
    used_by_spec = {
        spec.id: {
            *[canonical_field(str(item)) for item in spec.dimensions],
            *[
                canonical_field(str(item))
                for item in spec.output_projection
            ],
            *[canonical_field(str(item.field)) for item in spec.filters],
        }
        for spec in query_specs
    }
    explicit_dates = {
        str(item.get("raw_span") or "")
        for item in evidence
        if item.get("rule_id") == "explicit_date"
        and str(item.get("raw_span") or "").strip()
    }
    has_comparison_hint = any(
        item.get("candidate_type") == "comparison_hint"
        for item in evidence
    )
    range_spans = [
        str(item.get("raw_span") or "")
        for item in evidence
        if item.get("candidate_type") == "time_range"
    ]
    # 两个日期写在一个连续时间段里（"A 到 B"）时，用户要的是整段，
    # 不能当成两次独立对照拆成单日节点——拆开等于丢掉中间日期。
    dates_inside_range = any(
        sum(1 for date in explicit_dates if date in span) >= 2
        for span in range_spans
    )
    if (
        len(explicit_dates) >= 2
        and has_comparison_hint
        and len(query_specs) < 2
        and not dates_inside_range
    ):
        return (
            "用户原话包含两个明确日期并要求比较或变化，"
            "必须为每个日期生成独立的 query_data 节点，"
            "不能压成一个连续区间；请保留原日期和用户范围。"
        )
    for spec in query_specs:
        used_fields = used_by_spec[spec.id]
        for item in evidence:
            discouraged = {
                str(field) for field in item.get("discouraged_fields") or ()
            }
            used = discouraged.intersection(used_fields)
            if not used:
                continue
            return (
                f"节点 {spec.id} 使用了不应承载该语义的字段 "
                f"{'、'.join(sorted(used))}。用户原话的语义候选字段是 "
                f"{item.get('candidate_field')}；"
                f"原因：{item.get('reason')}。"
                "请只修正该节点字段，保持用户范围不变。"
            )
    all_used = {
        field
        for fields in used_by_spec.values()
        for field in fields
    }
    for item in evidence:
        candidate_field = canonical_field(
            str(item.get("candidate_field") or "")
        )
        if not item.get("requires_field") or not candidate_field:
            continue
        if candidate_field in all_used:
            continue
        capability = None
        for spec in query_specs:
            capability = (
                registry.snapshot.fields.get(spec.subject, {}).get(
                    candidate_field
                )
                if registry is not None
                else None
            )
            if capability is not None:
                break
        supported_logical_field = (
            capability is not None
            or candidate_field
            in (
                registry.snapshot.query_shapes.get("semantic", {})
                .get("group_by", ())
            )
        ) if registry is not None else capability is not None
        if not supported_logical_field:
            continue
        if capability is not None and capability.status in {
            "unavailable",
            "lookup_required",
        }:
            raise QuerySpecUnsupported(
                "当前数据能力不支持："
                + (
                    capability.unavailable_reason
                    or "字段能力不可用"
                )
            )
        node_id = query_specs[0].id if query_specs else ""
        return (
            f"节点 {node_id} 丢失了用户明确要求涉及的字段 "
            f"{candidate_field}；请补回该字段并保持用户范围不变。"
        )
    return ""


def _query_plan_field_issue(
    plan: QueryPlan,
    registry: CapabilityRegistry | None,
) -> CapabilityIssue | None:
    if registry is None:
        return None
    for node in plan.nodes:
        fields = registry.snapshot.fields.get(node.spec.subject, {})
        referenced = {
            *[str(item).rsplit(".", 1)[-1] for item in node.spec.dimensions],
            *[
                str(item).rsplit(".", 1)[-1]
                for item in node.spec.output_projection
            ],
            *[
                str(item.field).rsplit(".", 1)[-1]
                for item in node.spec.filters
            ],
        }
        for field_id in sorted(referenced):
            field = fields.get(field_id)
            if field is None or field.status not in {
                "unavailable",
                "lookup_required",
            }:
                continue
            return CapabilityIssue(
                code="field_unavailable",
                original_value=field_id,
                reason=field.unavailable_reason or "字段能力不可用",
                node_id=node.id,
                evidence={
                    "subject": node.spec.subject,
                    "field": field_id,
                    "status": field.status,
                },
            )
    return None


def _complete_trend_dependencies(plan: QueryPlan) -> QueryPlan:
    data_ids = tuple(node.id for node in plan.nodes if node.node_type == "query_data")
    if not data_ids:
        return plan
    nodes = []
    for node in plan.nodes:
        if node.node_type == "post_calc" and node.spec.view == "trend":
            node = PlanNode(
                id=node.id,
                spec=node.spec,
                depends_on=tuple(
                    dict.fromkeys((*data_ids, *node.depends_on))
                ),
                node_type=node.node_type,
            )
        nodes.append(node)
    return QueryPlan(
        question=plan.question,
        nodes=tuple(nodes),
        response_mode=plan.response_mode,
    )


def _complete_trend_analysis(
    plan: QueryPlan,
    intent_family: str = "",
) -> QueryPlan:
    trend_data = [
        node
        for node in plan.nodes
        if node.node_type == "query_data" and node.spec.view == "trend"
    ]
    if not trend_data and intent_family == "trend":
        trend_data = [
            node
            for node in plan.nodes
            if node.node_type == "query_data"
        ]
    if not trend_data:
        return plan
    trend_ids = {node.id for node in trend_data}
    for node in plan.nodes:
        if (
            node.node_type != "post_calc"
            or not trend_ids.intersection(node.depends_on)
        ):
            continue
        if node.spec.view == "trend":
            return plan
        if "analysis_only" in node.spec.required_assertions:
            return replace(
                plan,
                nodes=tuple(
                    replace(
                        item,
                        spec=(
                            replace(item.spec, view="trend")
                            if item.id == node.id
                            else item.spec
                        ),
                    )
                    for item in plan.nodes
                ),
                response_mode="analysis",
            )
    used_ids = {node.id for node in plan.nodes}
    post_id = "pc_trend"
    index = 2
    while post_id in used_ids:
        post_id = f"pc_trend_{index}"
        index += 1
    first = trend_data[0].spec
    post_spec = QuerySpec(
        id=post_id,
        kind=first.kind,
        subject=first.subject,
        view="trend",
        metrics=(),
        dimensions=(),
        filters=(),
        output_projection=(),
        order_by=(),
        limit=None,
        required_assertions=("analysis_only", "preserve_user_scope"),
        question="分析依赖节点的时间序列，说明变化趋势并指出极值；只使用已有结果。",
    )
    return QueryPlan(
        question=plan.question,
        nodes=(
            *plan.nodes,
            PlanNode(
                id=post_id,
                spec=post_spec,
                depends_on=tuple(node.id for node in trend_data),
                node_type="post_calc",
            ),
        ),
        response_mode="analysis",
    )


def _complete_trend_calculation(plan: QueryPlan) -> QueryPlan:
    data_nodes = [
        node for node in plan.nodes if node.node_type == "query_data"
    ]
    if len(data_nodes) < 2:
        return plan
    batch_node = next(
        (
            node
            for node in data_nodes
            if node.spec.view == "list"
            and (node.spec.limit is not None or node.spec.order_by)
        ),
        None,
    )
    value_node = next(
        (
            node
            for node in data_nodes
            if node is not batch_node and node.spec.metrics
        ),
        None,
    )
    if batch_node is None or value_node is None:
        return plan
    label_field = next(
        iter(
            (
                *batch_node.spec.dimensions,
                *batch_node.spec.output_projection,
            )
        ),
        "",
    )
    value_field = value_node.spec.metrics[0] if value_node.spec.metrics else ""
    if not label_field or not value_field:
        return plan
    nodes = []
    for node in plan.nodes:
        calculation = node.spec.calculation
        if (
            node.node_type == "post_calc"
            and calculation is not None
            and calculation.tool == "calculate_series"
        ):
            calculation = replace(
                calculation,
                operands=(
                    CalculationOperand(
                        name="labels",
                        source={
                            "node": batch_node.id,
                            "field": label_field,
                        },
                    ),
                    CalculationOperand(
                        name="values",
                        source={
                            "node": value_node.id,
                            "field": value_field,
                        },
                    ),
                ),
            )
            node = replace(
                node,
                spec=replace(node.spec, calculation=calculation),
            )
        nodes.append(node)
    return QueryPlan(
        question=plan.question,
        nodes=tuple(nodes),
        response_mode=plan.response_mode,
    )


def _complete_scalar_metric_comparisons(
    plan: QueryPlan,
    registry: CapabilityRegistry | None,
) -> QueryPlan:
    comparisons = [
        node
        for node in plan.nodes
        if node.node_type == "post_calc"
        and node.spec.calculation is not None
        and node.spec.calculation.tool == "calculate_scalar"
        and len(node.depends_on) == 2
    ]
    if len(comparisons) != 1:
        return plan
    comparison = comparisons[0]
    nodes_by_id = {node.id: node for node in plan.nodes}
    dependencies = [
        nodes_by_id[dependency]
        for dependency in comparison.depends_on
        if dependency in nodes_by_id
    ]
    if len(dependencies) != 2 or any(
        node.node_type != "query_data"
        or node.spec.view not in {"count", "sum", "avg"}
        or node.spec.dimensions
        for node in dependencies
    ):
        return plan
    common_metrics = [
        metric
        for metric in dependencies[0].spec.metrics
        if metric in dependencies[1].spec.metrics
    ]
    calculation = comparison.spec.calculation
    roles = roles_for_tool(calculation.tool, calculation.operation)
    if roles is None or len(roles) != 2:
        return plan
    used_metrics = {
        str(operand.source.get("field") or "")
        for operand in calculation.operands
        if operand.source
    }
    missing_metrics = [
        metric for metric in common_metrics if metric not in used_metrics
    ]
    if not missing_metrics:
        return plan
    used_ids = {node.id for node in plan.nodes}
    additions: list[PlanNode] = []
    for metric in missing_metrics:
        metric_cap = (
            registry.metric(comparison.spec.subject, metric)
            if registry is not None
            else None
        )
        suffix = (
            "_change_pct"
            if calculation.operation == "percentage_change"
            else f"_{calculation.operation}"
        )
        output_name = f"{metric}{suffix}"
        node_id = f"{comparison.id}_{metric}"
        index = 2
        while node_id in used_ids:
            node_id = f"{comparison.id}_{metric}_{index}"
            index += 1
        used_ids.add(node_id)
        metric_label = (
            metric_cap.label if metric_cap is not None else metric
        )
        cloned_calculation = replace(
            calculation,
            operands=tuple(
                CalculationOperand(
                    name=role,
                    source={
                        "node": dependency.id,
                        "field": metric,
                    },
                )
                for role, dependency in zip(roles, dependencies)
            ),
            output=CalculationOutput(
                name=output_name,
                label=f"{metric_label}变化百分比"
                if calculation.operation == "percentage_change"
                else f"{metric_label}变化",
                unit="%" if calculation.operation == "percentage_change" else (
                    metric_cap.unit if metric_cap is not None else ""
                ),
            ),
        )
        additions.append(
            PlanNode(
                id=node_id,
                spec=replace(
                    comparison.spec,
                    id=node_id,
                    question=f"比较{metric_label}在两个周期的变化",
                    calculation=cloned_calculation,
                ),
                depends_on=comparison.depends_on,
                node_type="post_calc",
            )
        )
    return QueryPlan(
        question=plan.question,
        nodes=(*plan.nodes, *additions),
        response_mode=plan.response_mode,
    )


def _normalize_group_share_calculation(plan: QueryPlan) -> QueryPlan:
    nodes = []
    for node in plan.nodes:
        calculation = node.spec.calculation
        if (
            node.node_type != "post_calc"
            or calculation is None
            or calculation.tool != "calculate_scalar"
            or calculation.operation != "percentage_share"
        ):
            nodes.append(node)
            continue
        source_nodes = {
            str(operand.source.get("node") or "")
            for operand in calculation.operands
            if operand.source
        }
        if len(source_nodes) != 1 or source_nodes != set(node.depends_on):
            nodes.append(node)
            continue
        upstream = next(
            (
                item
                for item in plan.nodes
                if item.id in source_nodes
            ),
            None,
        )
        if upstream is None or not upstream.spec.dimensions:
            nodes.append(node)
            continue
        nodes.append(
            replace(
                node,
                spec=replace(
                    node.spec,
                    calculation=replace(
                        calculation,
                        tool="calculate_columns",
                    ),
                ),
            )
        )
    return QueryPlan(
        question=plan.question,
        nodes=tuple(nodes),
        response_mode=plan.response_mode,
    )


def _analyze_once(
    question: str,
    kind: str,
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
    planning_feedback: str = "",
    *,
    registry: CapabilityRegistry | None = None,
    subject: str = "",
) -> QueryPlan:
    original = str(question or "").strip()
    if not original:
        raise QuerySpecError("用户问题为空")
    planner_view = (
        registry.planner_view(subject, reach).to_prompt_dict()
        if registry is not None and subject
        else {}
    )
    assist_evidence = [
        item.to_dict()
        for item in detect_evidence(original, kind)
    ]
    data = chat_json(
        prompts.messages(
            "query_spec",
            question=original,
            kind=kind,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            capabilities=_text_context(planner_view, limit=24000),
            schema=_text_context(
                sorted(
                    (table, column)
                    for table, column in schema.columns
                    if planner_view
                    and table == planner_view.get("base_table")
                ),
                limit=5000,
            )
            if planner_view
            else _text_context(sorted(schema.columns)),
            planning_feedback=planning_feedback or "无",
            assist_evidence=_text_context(assist_evidence, limit=6000),
            reach=_text_context(
                [
                    {
                        "table": item.table,
                        "column": item.column,
                        "matched_text": item.matched_text,
                        "source": item.source,
                        "score": item.score,
                    }
                    for item in reach.hits
                ]
            ),
        ),
        model=role_model("semantic"),
        max_tokens=4096,
    )
    if not isinstance(data, dict):
        raise QuerySpecError("模型没有返回 QuerySpec JSON")
    contract = IntentContract(
        original_question=original,
        kind=kind,
        intent_family=str(data.get("intent_family") or "").strip(),
        final_goal=str(data.get("final_goal") or original).strip(),
        unknown_terms=_string_tuple(data.get("unknown_terms")),
    )
    specs: list[QuerySpec] = []
    dependencies: dict[str, tuple[str, ...]] = {}
    node_types: dict[str, str] = {}
    raw_views = data.get("views")
    if not isinstance(raw_views, list) or not raw_views:
        # 模型不产出视图时，如果问题是踩在已声明不可用的能力上
        # （例如车道分布），要说清是哪项能力缺失，而不是笼统说“没产出视图”。
        unavailable = _unavailable_assist_reason(assist_evidence, registry)
        if unavailable:
            raise QuerySpecUnsupported(unavailable)
        if contract.unknown_terms:
            # 规划器按契约把"当前能力覆盖不到什么"写进了 unknown_terms
            # （例如只注册了 500 米 / 1 公里两种分段粒度），要原样作为
            # 不支持原因返回，而不是暴露"缺少 views"这种内部错误。
            raise QuerySpecUnsupported("；".join(contract.unknown_terms))
        raise QuerySpecError("QuerySpec 缺少 views")
    for index, raw in enumerate(raw_views, 1):
        if not isinstance(raw, dict):
            continue
        spec_id = str(raw.get("id") or f"q{index}").strip()
        selected_subject = str(raw.get("subject") or subject or "disease").strip()
        filters = []
        for item in raw.get("filters") or []:
            if not isinstance(item, dict):
                raise QuerySpecError("过滤条件必须是对象")
            filters.append(
                resolve_value(
                    str(item.get("field") or ""),
                    str(item.get("op") or "="),
                    item.get("raw_value", item.get("value")),
                    original,
                    kind,
                    subject=selected_subject,
                    registry=registry,
                )
            )
        try:
            limit = int(raw["limit"]) if raw.get("limit") is not None else None
        except (TypeError, ValueError) as exc:
            raise QuerySpecError("limit 必须是整数") from exc
        action = str(raw.get("action") or "query_data").strip()
        if action == "analysis":
            action = "post_calc"
        node_type = (
            action
            if action in {"query_data", "post_calc", "analysis"}
            else "query_data"
        )
        metrics = _metric_tuple(raw.get("metrics"))
        dimensions = _canonical_dimensions(
            selected_subject,
            _string_tuple(raw.get("dimensions")),
        )
        view = _canonical_view(
            raw.get("view"),
            metrics,
            dimensions,
            node_type,
        )
        if (
            node_type == "post_calc"
            and view in {"metrics", "derived_metrics", "post_calc"}
        ):
            view = "data"
        if view in {"list", "detail"} and limit is None:
            limit = 500
        output_projection = _canonical_dimensions(
            selected_subject,
            _string_tuple(raw.get("output_projection")),
        )
        input_bindings = []
        for item in raw.get("inputs") or []:
            if not isinstance(item, dict):
                raise QuerySpecError("inputs 必须是对象数组")
            input_bindings.append(
                InputBinding(
                    from_node=str(item.get("from_node") or "").strip(),
                    from_field=str(item.get("from_field") or "").strip(),
                    to_field=str(item.get("to_field") or "").strip(),
                    op=str(item.get("op") or "in").strip(),
                )
            )
        raw_calculation = raw.get("calculation")
        if raw_calculation is not None and not isinstance(
            raw_calculation,
            dict,
        ):
            raise QuerySpecError(
                "calculation_output_invalid: calculation 必须是对象"
            )
        try:
            calculation = (
                CalculationRequest.from_dict(raw_calculation)
                if isinstance(raw_calculation, dict)
                else None
            )
        except (TypeError, ValueError) as exc:
            raise QuerySpecError(
                f"calculation_output_invalid: {exc}"
            ) from exc
        normalized_spec = QuerySpec(
            id=spec_id,
            kind=kind,
            subject=selected_subject,
            view=view,
            metrics=metrics,
            dimensions=dimensions,
            filters=tuple(filters),
            output_projection=output_projection,
            order_by=_order_items(raw.get("order_by")),
            limit=limit,
            required_assertions=_string_tuple(raw.get("required_assertions")),
            question=str(raw.get("question") or original).strip(),
            input_bindings=tuple(input_bindings),
            calculation=calculation,
        )
        specs.append(
            normalize_top_view(normalized_spec)
            if node_type == "query_data"
            else normalized_spec
        )
        dependencies[spec_id] = _string_tuple(raw.get("depends_on"))
        node_types[spec_id] = node_type
    _canonicalize_input_bindings(specs)
    assist_issue = _assist_field_issue(
        specs,
        assist_evidence,
        registry,
        node_types,
    )
    if assist_issue:
        raise QuerySpecError(assist_issue)
    plan = build_query_plan(
        contract,
        tuple(specs),
        schema,
        reach,
        dependencies,
        node_types,
        response_mode=(
            "analysis"
            if contract.intent_family in {"analysis", "trend"}
            else "data"
        ),
    )
    requires_order = any(
        str(item.get("candidate_type") or "") == "order_hint"
        for item in assist_evidence
    )
    for spec in specs:
        order_issue = _spec_order_issue(spec, registry)
        if order_issue:
            raise QuerySpecError(order_issue)
        if requires_order:
            extreme_issue = _spec_extreme_order_issue(spec, node_types)
            if extreme_issue:
                raise QuerySpecError(extreme_issue)
    plan = _complete_trend_analysis(plan, contract.intent_family)
    plan = _complete_trend_dependencies(plan)
    plan = _complete_trend_calculation(plan)
    plan = _complete_scalar_metric_comparisons(plan, registry)
    plan = _normalize_group_share_calculation(plan)
    plan = normalize_named_period_window(
        plan,
        [
            str(item.get("raw_span") or "")
            for item in assist_evidence
            if item.get("rule_id") == "named_period"
        ],
        datetime.now(),
    )
    subject_issue = _plan_subject_issue(plan, subject)
    if subject_issue:
        raise QuerySpecError(subject_issue)
    intent_issue = _plan_intent_issue(plan, contract.intent_family)
    if intent_issue:
        raise QuerySpecError(intent_issue)
    shaping_issue = _plan_shaping_issue(plan, contract.intent_family)
    if shaping_issue:
        raise QuerySpecError(shaping_issue)
    return plan


def _apply_audit_patches(plan: QueryPlan, verdict: dict[str, Any]) -> QueryPlan:
    allowed = {
        "limit",
        "view",
        "metrics",
        "dimensions",
        "order_by",
        "output_projection",
        "required_assertions",
    }
    patch_by_node: dict[str, dict[str, Any]] = {}
    for issue in verdict.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        node_id = str(issue.get("node_id") or "")
        changes = issue.get("set")
        if node_id and isinstance(changes, dict):
            patch_by_node.setdefault(node_id, {}).update(
                {
                    key: value
                    for key, value in changes.items()
                    if key in allowed
                }
            )
    if not patch_by_node:
        return plan
    nodes = []
    for node in plan.nodes:
        changes = patch_by_node.get(node.id)
        if not changes:
            nodes.append(node)
            continue
        values = dict(node.spec.__dict__)
        values.update(changes)
        if "metrics" in changes:
            values["metrics"] = _metric_tuple(changes["metrics"])
        if "dimensions" in changes:
            values["dimensions"] = _canonical_dimensions(
                str(values.get("subject") or ""),
                _string_tuple(changes["dimensions"]),
            )
        if "output_projection" in changes:
            values["output_projection"] = _canonical_dimensions(
                str(values.get("subject") or ""),
                _string_tuple(changes["output_projection"]),
            )
        if "order_by" in changes:
            values["order_by"] = _order_items(changes["order_by"])
        if "required_assertions" in changes:
            values["required_assertions"] = _string_tuple(
                changes["required_assertions"]
            )
        if "view" in changes:
            values["view"] = _canonical_view(
                changes["view"],
                tuple(values.get("metrics") or ()),
                tuple(values.get("dimensions") or ()),
                node.node_type,
            )
        nodes.append(
            node.__class__(
                id=node.id,
                spec=(
                    normalize_top_view(QuerySpec(**values))
                    if node.node_type == "query_data"
                    else QuerySpec(**values)
                ),
                depends_on=node.depends_on,
                node_type=node.node_type,
            )
        )
    return QueryPlan(
        question=plan.question,
        nodes=tuple(nodes),
        response_mode=plan.response_mode,
    )


def _plan_contract_issues(
    plan: QueryPlan,
    schema: PhysicalSchemaView,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for node in plan.nodes:
        for issue in validate_query_spec(
            node.spec,
            schema,
            OutputProjection(fields=()),
            node_type=node.node_type,
        ):
            issues.append(
                {
                    "code": issue.code,
                    "node_id": node.id,
                    "reason": issue.message,
                    "repair_hint": "",
                }
            )
    return issues


def _audit_issue_changes_plan(plan: QueryPlan, issue: dict[str, Any]) -> bool:
    changes = issue.get("set")
    if not isinstance(changes, dict) or not changes:
        return True
    return _apply_audit_patches(plan, {"issues": [issue]}) != plan


def _query_plan_compile_error(plan: QueryPlan) -> CapabilityIssue | None:
    for node in plan.nodes:
        if node.node_type != "query_data":
            continue
        if node.spec.subject not in {"disease", "patrol"}:
            continue
        try:
            sql = compile_spec_mdl(node.spec)
        except UnsupportedQuerySpec as exc:
            message = str(exc) or f"节点 {node.id} 无法编译"
            return _capability_issue_from_compile_error(
                node.id,
                message,
            )
        if not sql:
            return CapabilityIssue(
                code="empty_compilation",
                original_value=node.id,
                reason=f"节点 {node.id} 编译结果为空",
                node_id=node.id,
            )
    return None


def _node_outputs(spec: QuerySpec) -> set[str]:
    return {
        *spec.metrics,
        *spec.dimensions,
        *spec.output_projection,
    }


def _query_plan_derived_metric_issue(
    plan: QueryPlan,
    registry: CapabilityRegistry | None,
) -> CapabilityIssue | None:
    if registry is None:
        return None
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        if node.node_type != "post_calc":
            continue
        dependency_subjects = {
            nodes_by_id[dependency].spec.subject
            for dependency in node.depends_on
            if dependency in nodes_by_id
        }
        capability_subject = (
            next(iter(dependency_subjects))
            if len(dependency_subjects) == 1
            else node.spec.subject
        )
        allowed_atomic = tuple(
            registry.snapshot.metrics.get(capability_subject, {})
        )
        allowed_derived = tuple(
            registry.snapshot.derived_metrics.get(capability_subject, {})
        )
        declared_derived = any(
            registry.derived_metric(capability_subject, metric_id) is not None
            for metric_id in node.spec.metrics
        )
        has_calculation_output = (
            node.spec.calculation is not None
            and node.spec.calculation.output is not None
            and node.spec.calculation.output.name in node.spec.metrics
        )
        if (
            "lower_is_better" in node.spec.required_assertions
            and allowed_derived
            and not declared_derived
            and not has_calculation_output
        ):
            return CapabilityIssue(
                code="missing_derived_metric",
                original_value=node.id,
                reason=(
                    f"节点 {node.id} 声明了越低越好的评价，"
                    "但没有声明使用的派生指标"
                ),
                allowed_values=allowed_derived,
                suggested_repair=(
                    "从 allowed_values 中选择一个派生指标写入 post_calc.metrics，"
                    "并确保它的 operands 全部由依赖节点产出。"
                ),
                node_id=node.id,
                evidence={
                    "capability_subject": capability_subject,
                    "post_calc_subject": node.spec.subject,
                    "required_assertions": list(node.spec.required_assertions),
                    "available_derived_metrics": {
                        metric_id: {
                            "expression": metric.expression,
                            "operands": list(metric.operands),
                        }
                        for metric_id, metric in registry.snapshot.derived_metrics[
                            capability_subject
                        ].items()
                    },
                },
            )
        produced: set[str] = set()
        for dependency in node.depends_on:
            upstream = nodes_by_id.get(dependency)
            if upstream is not None:
                produced.update(_node_outputs(upstream.spec))
        for metric_id in node.spec.metrics:
            if (
                node.spec.calculation is not None
                and node.spec.calculation.output is not None
                and metric_id == node.spec.calculation.output.name
            ):
                continue
            if registry.metric(capability_subject, metric_id) is not None:
                continue
            derived = registry.derived_metric(capability_subject, metric_id)
            if derived is None:
                return CapabilityIssue(
                    code="unknown_metric",
                    original_value=metric_id,
                    reason=(
                        f"节点 {node.id} 使用了未注册的指标 {metric_id}"
                    ),
                    allowed_values=allowed_atomic + allowed_derived,
                    suggested_repair=(
                        "只使用 allowed_values 中的指标；派生指标必须由 post_calc 计算。"
                    ),
                    node_id=node.id,
                )
            missing = tuple(
                operand for operand in derived.operands if operand not in produced
            )
            if missing:
                return CapabilityIssue(
                    code="missing_derived_operand",
                    original_value=metric_id,
                    reason=(
                        f"派生指标 {metric_id} 缺少上游操作数："
                        + "、".join(missing)
                    ),
                    allowed_values=missing,
                    suggested_repair=(
                        f"在依赖节点 {', '.join(node.depends_on) or '（未声明）'} "
                        f"的 metrics 中加入 {'、'.join(missing)}，"
                        f"再由 post_calc 计算 {derived.expression}。"
                    ),
                    node_id=node.id,
                    evidence={
                        "capability_subject": capability_subject,
                        "post_calc_subject": node.spec.subject,
                        "derived_metric": metric_id,
                        "operands": list(derived.operands),
                        "missing_operands": list(missing),
                        "dependency_node_ids": list(node.depends_on),
                        "produced_values": sorted(produced),
                        "expression": derived.expression,
                    },
                )
    return None


def _query_plan_calculation_issue(
    plan: QueryPlan,
    registry: CapabilityRegistry | None,
) -> CapabilityIssue | None:
    if registry is None:
        return None
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        calculation = node.spec.calculation
        if calculation is None:
            continue
        if node.node_type != "post_calc":
            return CapabilityIssue(
                code="calculation_output_invalid",
                original_value=node.id,
                reason="calculation 只能用于 post_calc 节点",
                node_id=node.id,
            )
        if (
            calculation is None
            and "analysis_only" not in node.spec.required_assertions
        ):
            return CapabilityIssue(
                code="calculation_required",
                original_value=node.id,
                reason=(
                    "post_calc 必须声明 calculation 工具和参数；"
                    "只解释已有值时声明 analysis_only"
                ),
                suggested_repair=(
                    "使用 calculate_scalar、calculate_columns 或 calculate_series。"
                ),
                node_id=node.id,
            )
        if calculation is None:
            continue
        capability = registry.snapshot.calculators.get(calculation.tool)
        if not isinstance(capability, Mapping):
            return CapabilityIssue(
                code="calculation_tool_unknown",
                original_value=calculation.tool,
                reason=f"未注册计算工具：{calculation.tool}",
                allowed_values=tuple(registry.snapshot.calculators),
                suggested_repair="只能使用能力视图 calculators 中登记的工具。",
                node_id=node.id,
            )
        operations = tuple(
            str(item) for item in capability.get("operations") or ()
        )
        if calculation.operation not in operations:
            return CapabilityIssue(
                code="calculation_operation_unknown",
                original_value=calculation.operation,
                reason=(
                    f"计算工具 {calculation.tool} 不支持操作符 "
                    f"{calculation.operation}"
                ),
                allowed_values=operations,
                suggested_repair="只能使用该工具登记的操作符。",
                node_id=node.id,
            )
        roles = roles_for_tool(
            calculation.tool,
            calculation.operation,
        )
        if roles is None:
            return CapabilityIssue(
                code="calculation_operation_unknown",
                original_value=calculation.operation,
                reason=f"未实现计算操作符：{calculation.operation}",
                node_id=node.id,
            )
        operand_names = tuple(item.name for item in calculation.operands)
        if len(set(operand_names)) != len(operand_names):
            return CapabilityIssue(
                code="calculation_operand_missing",
                original_value=calculation.operation,
                reason="计算操作数角色不能重复",
                allowed_values=roles,
                suggested_repair="每个命名操作数只能出现一次。",
                node_id=node.id,
                evidence={"actual_roles": list(operand_names)},
            )
        missing_roles = [role for role in roles if role not in operand_names]
        extra_roles = [role for role in operand_names if role not in roles]
        if missing_roles or extra_roles:
            return CapabilityIssue(
                code="calculation_operand_missing",
                original_value=calculation.operation,
                reason=(
                    "计算操作数角色不完整；缺少："
                    + ("、".join(missing_roles) or "无")
                    + "；多余："
                    + ("、".join(extra_roles) or "无")
                ),
                allowed_values=roles,
                suggested_repair="按操作符要求的命名角色传操作数。",
                node_id=node.id,
                evidence={
                    "required_roles": list(roles),
                    "actual_roles": list(operand_names),
                },
            )
        produced: dict[str, set[str]] = {}
        for dependency in node.depends_on:
            upstream = nodes_by_id.get(dependency)
            if upstream is not None:
                produced[dependency] = _node_outputs(upstream.spec)
        for operand in calculation.operands:
            if operand.source:
                source_node = str(operand.source.get("node") or "")
                source_field = str(operand.source.get("field") or "")
                if (
                    source_node not in produced
                    or source_field not in produced[source_node]
                ):
                    return CapabilityIssue(
                        code="calculation_operand_missing",
                        original_value=operand.name,
                        reason=(
                            f"操作数 {operand.name} 的来源 "
                            f"{source_node}.{source_field} 未被依赖节点产出"
                        ),
                        allowed_values=tuple(
                            sorted(
                                {
                                    field
                                    for fields in produced.values()
                                    for field in fields
                                }
                            )
                        ),
                        suggested_repair=(
                            "把来源改为依赖节点实际产出的 field，"
                            "或在对应数据节点增加该指标/维度。"
                        ),
                        node_id=node.id,
                        evidence={
                            "operand": operand.name,
                            "source_node": source_node,
                            "source_field": source_field,
                            "dependency_node_ids": list(node.depends_on),
                        },
                    )
            elif operand.value is None:
                return CapabilityIssue(
                    code="calculation_operand_missing",
                    original_value=operand.name,
                    reason=f"操作数 {operand.name} 缺少 value 或 source",
                    node_id=node.id,
                )
        source_keys = [
            (
                str(operand.source.get("node") or ""),
                str(operand.source.get("field") or ""),
            )
            for operand in calculation.operands
            if operand.source
        ]
        same_source_group_share = (
            calculation.tool == "calculate_columns"
            and calculation.operation == "percentage_share"
            and len(source_keys) == 2
            and source_keys[0] == source_keys[1]
        )
        if (
            len(source_keys) != len(set(source_keys))
            and not same_source_group_share
        ):
            return CapabilityIssue(
                code="calculation_operand_missing",
                original_value=calculation.operation,
                reason="不同操作数不能引用相同的上游来源",
                suggested_repair=(
                    "为每个操作数指定不同节点或不同字段；"
                    "若总数已由独立聚合节点产出，直接使用该节点。"
                ),
                node_id=node.id,
                evidence={"source_keys": source_keys},
            )
        if calculation.tool == "calculate_series":
            source_nodes = {
                str(operand.source.get("node") or "")
                for operand in calculation.operands
                if operand.source
            }
            source_specs = [
                nodes_by_id[node_id]
                for node_id in source_nodes
                if node_id in nodes_by_id
            ]
            scalar_sources = all(
                source.spec.view in {"count", "sum", "avg"}
                and not source.spec.dimensions
                for source in source_specs
            )
            if len(source_specs) >= 2 and scalar_sources:
                return CapabilityIssue(
                    code="calculation_tool_shape_mismatch",
                    original_value=calculation.tool,
                    reason=(
                        "calculate_series 只能处理有序序列；"
                        "当前两个操作数都是单值节点。"
                    ),
                    allowed_values=tuple(registry.snapshot.calculators),
                    suggested_repair=(
                        "两个标量周期比较改用 calculate_scalar："
                        "percentage_change 使用 old/new；"
                        "差值使用 left/right。"
                    ),
                    node_id=node.id,
                    evidence={
                        "source_views": {
                            source.id: source.spec.view
                            for source in source_specs
                        },
                    },
                )
        if (
            calculation.tool == "calculate_columns"
            and (
                calculation.output is None
                or not calculation.output.name
            )
        ):
            return CapabilityIssue(
                code="calculation_output_invalid",
                original_value=node.id,
                reason="calculate_columns 必须声明 output.name",
                node_id=node.id,
            )
    return None


def _capability_issue_from_compile_error(
    node_id: str,
    message: str,
) -> CapabilityIssue:
    if "不支持指标：" in message:
        original = message.split("不支持指标：", 1)[1].split("；", 1)[0]
        allowed_text = message.split("可用指标：", 1)[1] if "可用指标：" in message else ""
        allowed = tuple(
            item.strip()
            for item in allowed_text.split("、")
            if item.strip()
        )
        return CapabilityIssue(
            code="unknown_metric",
            original_value=original,
            reason=message,
            allowed_values=allowed,
            suggested_repair=(
                "只使用 allowed_values 中的原子指标；派生指标改用 post_calc。"
            ),
            node_id=node_id,
        )
    return CapabilityIssue(
        code="unsupported_capability",
        original_value=node_id,
        reason=message,
        node_id=node_id,
    )


def _query_plan_compiles(plan: QueryPlan) -> bool:
    return _query_plan_compile_error(plan) is None


def _drop_redundant_invalid_post_calc(
    plan: QueryPlan,
    issue: CapabilityIssue,
) -> QueryPlan | None:
    if not issue.code.startswith("calculation_") or not issue.node_id:
        return None
    target = next(
        (node for node in plan.nodes if node.id == issue.node_id),
        None,
    )
    if target is None or target.node_type != "post_calc":
        return None
    dependency_ids = set(target.depends_on)
    has_independent_aggregate = any(
        node.node_type == "query_data"
        and node.id not in dependency_ids
        and node.spec.view in {"count", "sum", "avg"}
        and not node.spec.dimensions
        for node in plan.nodes
    )
    remaining = tuple(node for node in plan.nodes if node.id != target.id)
    if not remaining:
        return None
    if not has_independent_aggregate:
        calculation = target.spec.calculation
        dependency_nodes = [
            node
            for node in plan.nodes
            if node.id in dependency_ids
            and node.node_type == "query_data"
        ]
        if (
            calculation is None
            or calculation.operation != "add"
            or len(dependency_nodes) != 1
            or not dependency_nodes[0].spec.dimensions
            or dependency_nodes[0].spec.metrics != ("count",)
        ):
            return None
        source = dependency_nodes[0].spec
        aggregate_id = "total"
        used_ids = {node.id for node in remaining}
        index = 2
        while aggregate_id in used_ids:
            aggregate_id = f"total_{index}"
            index += 1
        aggregate_spec = replace(
            source,
            id=aggregate_id,
            view="count",
            metrics=("count",),
            dimensions=(),
            output_projection=(),
            order_by=(),
            limit=None,
            question="统计同一筛选范围的记录总数",
        )
        return QueryPlan(
            question=plan.question,
            nodes=(
                *remaining,
                PlanNode(id=aggregate_id, spec=aggregate_spec),
            ),
            response_mode=plan.response_mode,
        )
    return QueryPlan(
        question=plan.question,
        nodes=remaining,
        response_mode=plan.response_mode,
    )


def _ambiguity_message(issues: list[Any]) -> str:
    reasons: list[str] = []
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or str(issue.get("code") or "") != "ambiguous_scope"
        ):
            continue
        reason = str(issue.get("reason") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return "；".join(reasons[:2])
    return "请补充具体时间范围、巡检批次、比较对象或所需字段。"


def analyze_single_turn(
    question: str,
    kind: str,
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
    *,
    audit: bool = False,
    registry: CapabilityRegistry | None = None,
    subject: str = "",
    planning_feedback: str = "",
) -> QueryPlan:
    feedback = str(planning_feedback or "")
    for attempt in range(3):
        try:
            plan = _analyze_once(
                question,
                kind,
                schema,
                reach,
                feedback,
                registry=registry,
                subject=subject,
            )
        except LLMError as exc:
            if attempt == 0:
                feedback = (
                    "上一轮模型没有返回合法 JSON。"
                    "请只输出符合 query_spec 格式的 JSON，不要附加解释。"
                    f"错误信息：{exc}"
                )
                continue
            raise
        except QuerySpecAmbiguity as exc:
            if attempt == 0:
                feedback = f"上一轮 QuerySpec 解析失败：{exc}"
                continue
            raise
        except QuerySpecUnsupported:
            raise
        except QuerySpecError as exc:
            feedback = f"上一轮 QuerySpec 解析失败：{exc}"
            if attempt < 2:
                continue
            raise
        if not audit:
            return plan
        compile_issue = _query_plan_field_issue(plan, registry)
        if compile_issue is None:
            compile_issue = _query_plan_compile_error(plan)
        if compile_issue is None:
            compile_issue = _query_plan_derived_metric_issue(plan, registry)
        if compile_issue is None:
            compile_issue = _query_plan_calculation_issue(plan, registry)
        if compile_issue:
            reduced_plan = _drop_redundant_invalid_post_calc(
                plan,
                compile_issue,
            )
            if reduced_plan is not None:
                plan = reduced_plan
                compile_issue = None
        if compile_issue:
            feedback = json.dumps(
                compile_issue.to_feedback(),
                ensure_ascii=False,
                default=str,
            )
            if compile_issue.code == "field_unavailable":
                raise QuerySpecError(
                    "当前数据能力不支持：" + compile_issue.reason
                )
            if attempt < 2:
                continue
            if compile_issue.code.startswith("calculation_"):
                raise QuerySpecError(
                    f"{compile_issue.code}: {compile_issue.reason}"
                )
            if compile_issue.code == "missing_derived_operand":
                raise QuerySpecError(
                    "缺少派生指标操作数：" + compile_issue.reason
                )
            raise QuerySpecError(
                "当前数据能力不支持：" + compile_issue.reason
            )
        verdict = audit_query_plan(str(question or "").strip(), plan)
        if verdict.get("complete") is True:
            return plan
        post_calc_ids = {
            node.id
            for node in plan.nodes
            if node.node_type == "post_calc"
        }
        consumed_by_post_calc = {
            dependency
            for node in plan.nodes
            if node.node_type == "post_calc"
            for dependency in node.depends_on
        }
        effective_issues = []
        for issue in verdict.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            node_id = str(issue.get("node_id") or "")
            code = str(issue.get("code") or "")
            if node_id in post_calc_ids:
                continue
            if (
                node_id in consumed_by_post_calc
                and code
                in {
                    "missing_metric",
                    "wrong_grouping",
                    "missing_input_binding",
                    "unused_dependency",
                }
            ):
                if not (
                    code == "missing_metric"
                    and isinstance(issue.get("set"), dict)
                    and issue["set"]
                ):
                    continue
            if (
                not node_id
                and post_calc_ids
                and code in {"missing_metric", "wrong_grouping"}
            ):
                continue
            effective_issues.append(issue)
        effective_issues = [
            issue
            for issue in effective_issues
            if _audit_issue_changes_plan(plan, issue)
        ]
        if not effective_issues:
            return plan
        verdict = {**verdict, "issues": effective_issues}
        patched = _apply_audit_patches(plan, verdict)
        if patched != plan:
            contract_issues = _plan_contract_issues(patched, schema)
            if contract_issues:
                verdict = {
                    "complete": False,
                    "issues": contract_issues,
                }
            else:
                if _query_plan_compiles(plan) and not _query_plan_compiles(patched):
                    return plan
                repaired = audit_query_plan(str(question or "").strip(), patched)
                if repaired.get("complete") is True:
                    return patched
                verdict = repaired
        issues = verdict.get("issues") or []
        feedback = json.dumps(issues, ensure_ascii=False, default=str)
        if any(
            isinstance(item, dict)
            and str(item.get("code") or "") == "ambiguous_scope"
            for item in issues
        ):
            raise QuerySpecAmbiguity(_ambiguity_message(issues))
    raise QuerySpecAmbiguity(
        _ambiguity_message(issues)
    )
