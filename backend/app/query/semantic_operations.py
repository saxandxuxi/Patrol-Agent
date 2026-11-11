from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .capability_registry import CapabilityIssue, CapabilityRegistry
from .comparison_contract import dataset_comparison_contract_issue
from .contracts import QueryPlan


ENTITY_SET_COMPARISON = "entity_set_comparison"
SCALAR_DIFFERENCE = "scalar_difference"
SCALAR_WITH_DETAILS = "scalar_with_details"
GROUP_SHARE = "group_share"
GROUP_TOTAL = "group_total"
GROUP_CHANGE = "group_change"


def answer_operation_catalog(
    registry: CapabilityRegistry | None,
) -> dict[str, Any]:
    if registry is None:
        return {}
    semantics = registry.snapshot.query_shapes.get("planner_semantics") or {}
    if not isinstance(semantics, Mapping):
        return {}
    operations = semantics.get("answer_operations") or {}
    return dict(operations) if isinstance(operations, Mapping) else {}


def semantic_operation_contract_issue(
    plan: QueryPlan,
    answer_operation: Any,
    registry: CapabilityRegistry | None,
) -> CapabilityIssue | None:
    operation = str(answer_operation or "").strip()
    if not operation or registry is None:
        return None

    catalog = answer_operation_catalog(registry)
    if catalog and operation not in catalog:
        return CapabilityIssue(
            code="unknown_answer_operation",
            original_value=operation,
            reason=f"语义层返回了未注册的查询操作：{operation}",
            allowed_values=tuple(catalog),
            suggested_repair="从 answer_operations 中选择一个已登记操作。",
            evidence={"available_operations": catalog},
        )

    if operation == SCALAR_WITH_DETAILS:
        contract = catalog.get(operation) or {}
        scalar_views = {
            str(item)
            for item in contract.get("scalar_views") or ()
        } or {"count", "sum", "avg"}
        detail_views = {
            str(item)
            for item in contract.get("detail_views") or ()
        } or {"list", "detail"}
        aggregates = [
            node
            for node in plan.nodes
            if node.node_type == "query_data"
            and node.spec.view in scalar_views
            and node.spec.metrics
            and not node.spec.dimensions
        ]
        details = [
            node
            for node in plan.nodes
            if node.node_type == "query_data"
            and node.spec.view in detail_views
            and not node.spec.metrics
        ]
        if not aggregates:
            return CapabilityIssue(
                code="scalar_with_details_requires_aggregate",
                original_value=operation,
                reason=(
                    "用户同时要求总体值和对象明细，"
                    "但计划没有同范围的无维度聚合结果。"
                ),
                allowed_values=tuple(sorted(scalar_views)),
                suggested_repair=(
                    "保留一个同 subject、同 filters、有 metrics 且无 dimensions "
                    "的 query_data 作为总体值。"
                ),
            )
        if not details:
            return CapabilityIssue(
                code="scalar_with_details_requires_details",
                original_value=operation,
                reason=(
                    "用户同时要求总体值和对象明细，"
                    "但计划没有不聚合的对象明细结果。"
                ),
                allowed_values=tuple(sorted(detail_views)),
                suggested_repair=(
                    "新增同 subject、同 filters 的 list/detail 节点，"
                    "不能把截断聚合结果冒充逐条明细。"
                ),
            )
        same_scope_detail = next(
            (
                detail
                for detail in details
                for aggregate in aggregates
                if detail.spec.subject == aggregate.spec.subject
                and detail.spec.kind == aggregate.spec.kind
                and detail.spec.filters == aggregate.spec.filters
            ),
            None,
        )
        if same_scope_detail is None:
            return CapabilityIssue(
                code="scalar_with_details_scope_mismatch",
                original_value=operation,
                reason=(
                    "总体值和对象明细没有使用同一主体与同一过滤范围，"
                    "明细不能代表总体值所覆盖的对象集合。"
                ),
                suggested_repair=(
                    "让聚合与明细节点逐字段保留相同的 subject、kind 和 filters；"
                    "只允许两者使用不同的 view、metrics 和输出字段。"
                ),
                evidence={
                    "aggregate_node_ids": [
                        aggregate.id for aggregate in aggregates
                    ],
                    "detail_node_ids": [
                        detail.id for detail in details
                    ],
                },
            )
        return None

    if operation == SCALAR_DIFFERENCE:
        calculations = [
            node.spec.calculation
            for node in plan.nodes
            if node.node_type == "post_calc"
            and node.spec.calculation is not None
        ]
        if not calculations:
            return CapabilityIssue(
                code="scalar_difference_requires_calculation",
                original_value=operation,
                reason=(
                    "用户要求差值，但计划没有声明计算工具，"
                    "不能用 analysis_only 口头拒绝或心算。"
                ),
                allowed_values=(
                    "calculate_scalar/subtract",
                    "calculate_series/subtract",
                ),
                suggested_repair=(
                    "保留上游真实计数，新增 post_calc 并声明 "
                    "calculate_scalar 或 calculate_series 的 subtract 操作，"
                    "操作数必须引用 depends_on 中的真实产出字段。"
                ),
                evidence={
                    "node_types": [
                        {
                            "id": node.id,
                            "type": node.node_type,
                            "view": node.spec.view,
                        }
                        for node in plan.nodes
                    ]
                },
            )
        if any(
            calculation.operation == "subtract"
            and calculation.tool
            in {"calculate_scalar", "calculate_series"}
            for calculation in calculations
        ):
            return None
        return CapabilityIssue(
            code="scalar_difference_requires_subtract",
            original_value=operation,
            reason="用户要求差值，但现有计算节点没有执行 subtract。",
            allowed_values=(
                "calculate_scalar/subtract",
                "calculate_series/subtract",
            ),
            suggested_repair=(
                "把差值计算改为 calculate_scalar 的 subtract，"
                "或对有序批次序列使用 calculate_series 的 subtract。"
            ),
            evidence={
                "calculations": [
                    {
                        "tool": calculation.tool,
                        "operation": calculation.operation,
                    }
                    for calculation in calculations
                ]
            },
        )

    if operation == GROUP_SHARE:
        calculations = [
            node.spec.calculation
            for node in plan.nodes
            if node.node_type == "post_calc"
            and node.spec.calculation is not None
        ]
        if any(
            calculation.tool == "calculate_columns"
            and calculation.operation in {"ratio", "percentage_share"}
            for calculation in calculations
        ):
            return None
        return CapabilityIssue(
            code="group_share_requires_columns",
            original_value=operation,
            reason=(
                "用户要求每个分组分别计算占比，"
                "现有计算节点没有逐行执行 ratio 或 percentage_share。"
            ),
            allowed_values=(
                "calculate_columns/ratio",
                "calculate_columns/percentage_share",
            ),
            suggested_repair=(
                "保留分组结果和真实总量，新增或修正 post_calc，"
                "使用 calculate_columns 的 percentage_share（百分数）"
                "或 ratio（分数）计算每一行；不得只取第一行或让模型心算。"
            ),
            evidence={
                "calculations": [
                    {
                        "tool": calculation.tool,
                        "operation": calculation.operation,
                    }
                    for calculation in calculations
                ]
            },
        )

    if operation == GROUP_CHANGE:
        calculations = [
            node.spec.calculation
            for node in plan.nodes
            if node.node_type == "post_calc"
            and node.spec.calculation is not None
        ]
        if any(
            calculation.tool == "calculate_group_change"
            and calculation.operation
            in {"subtract", "percentage_change"}
            for calculation in calculations
        ):
            return None
        return CapabilityIssue(
            code="group_change_requires_calculation",
            original_value=operation,
            reason=(
                "用户要求比较两次分组结果的变化，"
                "但计划没有用分组变化工具对齐并计算。"
            ),
            allowed_values=(
                "calculate_group_change/subtract",
                "calculate_group_change/percentage_change",
            ),
            suggested_repair=(
                "保留前次和后次两个同形分组节点，"
                "新增 post_calc，使用 calculate_group_change。"
                "labels 指向共同分组字段，before/after 分别指向两次指标。"
            ),
            evidence={
                "calculations": [
                    {
                        "tool": calculation.tool,
                        "operation": calculation.operation,
                    }
                    for calculation in calculations
                ]
            },
        )

    if operation == GROUP_TOTAL:
        grouped_nodes = [
            node
            for node in plan.nodes
            if node.node_type == "query_data"
            and node.spec.view in {
                "distribution",
                "top_groups",
                "trend",
            }
            and node.spec.dimensions
            and node.spec.metrics
        ]
        if not grouped_nodes:
            return CapabilityIssue(
                code="group_total_requires_grouped_result",
                original_value=operation,
                reason=(
                    "用户同时要求分组结果和总体值，"
                    "但计划没有可执行的分组结果节点。"
                ),
                allowed_values=(
                    "distribution",
                    "top_groups",
                    "trend",
                ),
                suggested_repair=(
                    "先保留真实分组维度、指标和用户范围，"
                    "再为同一范围补充无分组的总体聚合。"
                ),
            )
        grouped = grouped_nodes[0]
        aggregate = next(
            (
                node
                for node in plan.nodes
                if node.node_type == "query_data"
                and node.spec.subject == grouped.spec.subject
                and node.spec.kind == grouped.spec.kind
                and not node.spec.dimensions
                and node.spec.filters == grouped.spec.filters
                and node.spec.metrics == grouped.spec.metrics
            ),
            None,
        )
        if aggregate is not None:
            summary = next(
                (
                    node
                    for node in plan.nodes
                    if node.node_type == "post_calc"
                    and "analysis_only"
                    in node.spec.required_assertions
                    and grouped.id in node.depends_on
                    and aggregate.id not in node.depends_on
                ),
                None,
            )
            if summary is not None:
                return CapabilityIssue(
                    code="group_total_summary_missing_total",
                    original_value=operation,
                    reason=(
                        "汇总节点依赖了分组结果，却没有消费已存在的"
                        "真实总体聚合节点，仍可能让分析模型心算总数。"
                    ),
                    allowed_values=(
                        f"depends_on 同时包含 {grouped.id} 和 {aggregate.id}",
                    ),
                    suggested_repair=(
                        f"把 {aggregate.id} 加入 {summary.id} 的 depends_on；"
                        "汇总节点只能复述真实总体值，不得新算数字。"
                    ),
                    node_id=summary.id,
                    evidence={
                        "grouped_node_id": grouped.id,
                        "aggregate_node_id": aggregate.id,
                        "current_depends_on": list(summary.depends_on),
                    },
                )
            return None
        return CapabilityIssue(
            code="group_total_requires_aggregate",
            original_value=operation,
            reason=(
                "分组结果已经存在，但缺少与它同主体、"
                "同过滤范围、同指标且无分组维度的总体聚合。"
            ),
            allowed_values=(
                "同范围无维度聚合节点",
            ),
            suggested_repair=(
                "新增一个 query_data 节点：保留分部节点的 subject、"
                "kind、filters 和 metrics，清除 dimensions，"
                "用于返回真实总体值；不得让 analysis_only 心算。"
            ),
            node_id=grouped.id,
            evidence={
                "grouped_node": {
                    "id": grouped.id,
                    "subject": grouped.spec.subject,
                    "filters": [
                        item.to_dict()
                        if hasattr(item, "to_dict")
                        else item
                        for item in grouped.spec.filters
                    ],
                    "metrics": list(grouped.spec.metrics),
                }
            },
        )

    if operation != ENTITY_SET_COMPARISON:
        return None

    comparison_nodes = [
        node
        for node in plan.nodes
        if node.node_type == "post_calc"
        and "dataset_comparison" in node.spec.required_assertions
    ]
    if not comparison_nodes:
        return CapabilityIssue(
            code="entity_set_comparison_requires_dataset_node",
            original_value=operation,
            reason=(
                "用户要求比较两次巡检之间新增或消失的实体，"
                "但计划没有生成实体集合比较节点"
            ),
            allowed_values=("dataset_comparison",),
            suggested_repair=(
                "为两个真实巡检批次各生成一个同形 list/detail 节点，"
                "输出共同身份字段（病害用 problem_number），"
                "再由 post_calc 声明 dataset_comparison；"
                "禁止用两个 COUNT 相减代替集合差集。"
            ),
            evidence={
                "node_types": [
                    {
                        "id": node.id,
                        "type": node.node_type,
                        "view": node.spec.view,
                        "metrics": list(node.spec.metrics),
                    }
                    for node in plan.nodes
                ]
            },
        )

    return dataset_comparison_contract_issue(plan, registry)


def semantic_operation_audit_issue(
    plan: QueryPlan,
    verdict: Mapping[str, Any],
    registry: CapabilityRegistry | None,
) -> CapabilityIssue | None:
    return semantic_operation_contract_issue(
        plan,
        verdict.get("answer_operation"),
        registry,
    )
