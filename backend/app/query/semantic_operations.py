from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .capability_registry import CapabilityIssue, CapabilityRegistry
from .comparison_contract import dataset_comparison_contract_issue
from .contracts import QueryPlan


ENTITY_SET_COMPARISON = "entity_set_comparison"


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
