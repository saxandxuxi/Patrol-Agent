from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .capability_registry import CapabilityIssue, CapabilityRegistry
from .comparison_contract import dataset_comparison_contract_issue
from .contracts import QueryPlan


ENTITY_SET_COMPARISON = "entity_set_comparison"
SCALAR_DIFFERENCE = "scalar_difference"
GROUP_SHARE = "group_share"


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
