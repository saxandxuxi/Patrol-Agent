from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .capability_registry import CapabilityIssue, CapabilityRegistry
from .contracts import QueryPlan


def _base_field(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _identity_fields(registry: CapabilityRegistry, subject: str) -> tuple[str, ...]:
    semantics = registry.snapshot.query_shapes.get("planner_semantics") or {}
    entity_comparison = semantics.get("entity_set_comparison") or {}
    if not isinstance(entity_comparison, Mapping):
        return ()
    raw = entity_comparison.get("identity_fields") or {}
    fields = raw.get(subject) if isinstance(raw, Mapping) else ()
    return tuple(
        _base_field(item)
        for item in fields or ()
        if _base_field(item)
    )


def _entity_candidates(
    node: Any,
    by_id: Mapping[str, Any],
    registry: CapabilityRegistry,
    allowed_views: set[str],
) -> list[tuple[str, tuple[str, ...]]]:
    by_subject: dict[str, list[tuple[str, set[str]]]] = {}
    for dependency_id in node.depends_on:
        dependency = by_id.get(dependency_id)
        if (
            dependency is None
            or dependency.node_type != "query_data"
            or dependency.spec.view not in allowed_views
        ):
            continue
        subject = str(dependency.spec.subject or "")
        identity_fields = _identity_fields(registry, subject)
        produced = {
            _base_field(item)
            for item in (
                *dependency.spec.dimensions,
                *dependency.spec.output_projection,
            )
        }
        matching = identity_fields and produced.intersection(identity_fields)
        if matching:
            by_subject.setdefault(subject, []).append(
                (dependency.id, set(matching))
            )
    candidates: list[tuple[str, tuple[str, ...]]] = []
    for subject, items in by_subject.items():
        if len(items) < 2:
            continue
        common = set.intersection(*(fields for _, fields in items))
        if not common:
            continue
        identity = next(
            (
                field
                for field in _identity_fields(registry, subject)
                if field in common
            ),
            sorted(common)[0],
        )
        candidates.append(
            (subject, tuple(item_id for item_id, _ in items))
        )
    return candidates


def infer_dataset_comparison_dependencies(
    plan: QueryPlan,
    node_id: str,
    registry: CapabilityRegistry,
) -> tuple[tuple[str, ...], CapabilityIssue | None]:
    semantics = registry.snapshot.query_shapes.get("planner_semantics") or {}
    entity_comparison = semantics.get("entity_set_comparison") or {}
    allowed_views = {
        str(item)
        for item in entity_comparison.get("allowed_dependency_views") or ()
    } if isinstance(entity_comparison, Mapping) else set()
    allowed_views = allowed_views or {"list", "detail"}
    by_id = {node.id: node for node in plan.nodes}
    node = by_id.get(node_id)
    if node is None:
        return (), None
    candidates = _entity_candidates(
        node,
        by_id,
        registry,
        allowed_views,
    )
    if len(candidates) == 1:
        return candidates[0][1], None
    if len(candidates) > 1:
        return (), CapabilityIssue(
            code="dataset_comparison_scope_ambiguous",
            original_value=node.id,
            reason=(
                "depends_on 中有多组可比较的实体节点；"
                "必须明确本次新增/消失比较的是哪一组"
            ),
            allowed_values=tuple(
                f"{subject}:{','.join(ids)}"
                for subject, ids in candidates
            ),
            suggested_repair=(
                "在 dataset_comparison 节点写入 comparison_depends_on，"
                "只列同一主体的两个同形明细节点。"
            ),
            node_id=node.id,
            evidence={
                "candidate_groups": [
                    {"subject": subject, "node_ids": list(ids)}
                    for subject, ids in candidates
                ],
            },
        )
    return (), None


def complete_dataset_comparison_dependencies(
    plan: QueryPlan,
    registry: CapabilityRegistry,
) -> QueryPlan:
    nodes = []
    changed = False
    for node in plan.nodes:
        if (
            node.node_type != "post_calc"
            or "dataset_comparison" not in node.spec.required_assertions
        ):
            nodes.append(node)
            continue
        comparison_ids = tuple(node.spec.comparison_depends_on)
        if not comparison_ids:
            inferred, issue = infer_dataset_comparison_dependencies(
                plan,
                node.id,
                registry,
            )
            if issue is None:
                comparison_ids = inferred
        dependency_ids = tuple(
            dict.fromkeys((*node.depends_on, *comparison_ids))
        )
        if (
            comparison_ids != node.spec.comparison_depends_on
            or dependency_ids != node.depends_on
        ):
            node = replace(
                node,
                spec=replace(
                    node.spec,
                    comparison_depends_on=comparison_ids,
                ),
                depends_on=dependency_ids,
            )
            changed = True
        nodes.append(node)
    return replace(plan, nodes=tuple(nodes)) if changed else plan


def normalize_dataset_comparison_order(plan: QueryPlan) -> QueryPlan:
    """让同一有序上游的差集按“较旧结果 → 较新结果”执行。

    `row_index=0` 表示排序结果第一行。若上游按时间倒序，则较大的
    `row_index` 更旧；差集工具会把“第二组减第一组”解释成新增。
    """

    by_id = {node.id: node for node in plan.nodes}

    def order_key(node: Any) -> tuple[str, int, bool] | None:
        if node.node_type != "query_data":
            return None
        indexed = [
            binding
            for binding in node.spec.input_bindings
            if binding.row_index is not None
        ]
        if len(indexed) != 1:
            return None
        binding = indexed[0]
        upstream = by_id.get(binding.from_node)
        if upstream is None:
            return None
        direction = next(
            (
                str(item.get("direction") or "").lower()
                for item in upstream.spec.order_by
                if isinstance(item, dict) and item.get("direction")
            ),
            "",
        )
        if direction not in {"asc", "desc"}:
            return None
        return binding.from_node, int(binding.row_index), direction == "desc"

    changed = False
    nodes = []
    for node in plan.nodes:
        if (
            node.node_type != "post_calc"
            or "dataset_comparison"
            not in node.spec.required_assertions
            or len(node.spec.comparison_depends_on) < 2
        ):
            nodes.append(node)
            continue
        dependencies = [
            by_id.get(dependency)
            for dependency in node.spec.comparison_depends_on
        ]
        keys = [
            order_key(dependency)
            if dependency is not None
            else None
            for dependency in dependencies
        ]
        if (
            any(key is None for key in keys)
            or len({key[0] for key in keys if key is not None}) != 1
            or len({key[2] for key in keys if key is not None}) != 1
        ):
            nodes.append(node)
            continue
        reverse = bool(keys[0][2])
        ordered = tuple(
            dependency.id
            for dependency in sorted(
                dependencies,
                key=lambda item: order_key(item)[1],
                reverse=reverse,
            )
        )
        if ordered != node.spec.comparison_depends_on:
            node = replace(
                node,
                spec=replace(
                    node.spec,
                    comparison_depends_on=ordered,
                ),
            )
            changed = True
        nodes.append(node)
    return replace(plan, nodes=tuple(nodes)) if changed else plan


def dataset_comparison_contract_issue(
    plan: QueryPlan,
    registry: CapabilityRegistry,
) -> CapabilityIssue | None:
    semantics = registry.snapshot.query_shapes.get("planner_semantics") or {}
    entity_comparison = semantics.get("entity_set_comparison") or {}
    if not isinstance(entity_comparison, Mapping):
        return None
    allowed_views = {
        str(item)
        for item in entity_comparison.get("allowed_dependency_views") or ()
    } or {"list", "detail"}
    by_id = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        if node.node_type != "post_calc":
            continue
        assertions = {
            str(item) for item in node.spec.required_assertions
        }
        if "dataset_comparison" not in assertions:
            continue
        dependencies = [
            by_id[dependency]
            for dependency in node.depends_on
            if dependency in by_id
        ]
        comparison_ids = tuple(node.spec.comparison_depends_on)
        if not comparison_ids:
            inferred, inference_issue = infer_dataset_comparison_dependencies(
                plan,
                node.id,
                registry,
            )
            if inference_issue is not None:
                return inference_issue
            comparison_ids = inferred
        if comparison_ids:
            missing_ids = [
                dependency
                for dependency in comparison_ids
                if dependency not in {item.id for item in dependencies}
            ]
            if missing_ids:
                return CapabilityIssue(
                    code="dataset_comparison_dependency_unknown",
                    original_value=node.id,
                    reason=(
                        "实体集合比较引用了不在 depends_on 中的数据节点："
                        + "、".join(missing_ids)
                    ),
                    allowed_values=tuple(item.id for item in dependencies),
                    suggested_repair=(
                        "把比较节点写入 comparison_depends_on，"
                        "同时保留在 depends_on 中。"
                    ),
                    node_id=node.id,
                )
            dependencies = [
                by_id[dependency] for dependency in comparison_ids
            ]
        if len(dependencies) < 2:
            return CapabilityIssue(
                code="dataset_comparison_requires_two_nodes",
                original_value=node.id,
                reason="实体集合比较必须依赖至少两个数据节点",
                suggested_repair=(
                    "为两个真实批次各生成一个同形 list/detail 数据节点，"
                    "再把它们交给 dataset_comparison。"
                ),
                node_id=node.id,
            )
        dependency_subjects = {
            dependency.spec.subject for dependency in dependencies
        }
        if len(dependency_subjects) != 1:
            return CapabilityIssue(
                code="dataset_comparison_subject_mismatch",
                original_value=node.id,
                reason="实体集合比较的数据节点必须属于同一个主体",
                allowed_values=tuple(sorted(dependency_subjects)),
                suggested_repair=(
                    "在 comparison_depends_on 中只选择同一主体的两个"
                    "同形明细节点，其他时间或汇总节点只作为支持依赖。"
                ),
                node_id=node.id,
                evidence={
                    "dependency_subjects": {
                        dependency.id: dependency.spec.subject
                        for dependency in dependencies
                    },
                },
            )
        subject = str(
            dependencies[0].spec.subject
            if comparison_ids
            else node.spec.subject or dependencies[0].spec.subject or ""
        )
        identity_fields = _identity_fields(registry, subject)
        if not identity_fields:
            return CapabilityIssue(
                code="dataset_comparison_identity_missing",
                original_value=subject,
                reason=f"主体 {subject} 没有登记可用于实体集合比较的身份字段",
                suggested_repair="在 query_shapes.entity_set_comparison.identity_fields 中登记真实身份字段。",
                node_id=node.id,
            )
        identity_sets: list[set[str]] = []
        for dependency in dependencies:
            if dependency.node_type != "query_data":
                return CapabilityIssue(
                    code="dataset_comparison_non_data_dependency",
                    original_value=dependency.id,
                    reason="实体集合比较的依赖必须是 query_data 节点",
                    node_id=node.id,
                )
            if dependency.spec.view not in allowed_views:
                return CapabilityIssue(
                    code="dataset_comparison_requires_entity_details",
                    original_value=dependency.spec.view,
                    reason=(
                        "实体集合比较不能用 COUNT 或聚合结果；"
                        f"节点 {dependency.id} 需要返回实体明细"
                    ),
                    allowed_values=tuple(sorted(allowed_views)),
                    suggested_repair=(
                        "改为 list/detail，并把实体身份字段放入 output_projection。"
                    ),
                    node_id=node.id,
                    evidence={
                        "dependency": dependency.id,
                        "view": dependency.spec.view,
                    },
                )
            produced = {
                _base_field(item)
                for item in (
                    *dependency.spec.dimensions,
                    *dependency.spec.output_projection,
                )
            }
            identity = next(
                (field for field in identity_fields if field in produced),
                "",
            )
            if not identity:
                return CapabilityIssue(
                    code="dataset_comparison_missing_identity",
                    original_value=dependency.id,
                    reason=(
                        f"节点 {dependency.id} 没有输出实体身份字段"
                        f"（可用：{'、'.join(identity_fields)}）"
                    ),
                    allowed_values=identity_fields,
                    suggested_repair=(
                        "把真实身份字段加入 output_projection；"
                        "不能只返回聚合指标。"
                    ),
                    node_id=node.id,
                    evidence={
                        "dependency": dependency.id,
                        "produced_fields": sorted(produced),
                    },
                )
            identity_sets.append({identity})
        common = set.intersection(*identity_sets) if identity_sets else set()
        if not common:
            return CapabilityIssue(
                code="dataset_comparison_identity_mismatch",
                original_value=node.id,
                reason="两个数据节点没有共同登记的实体身份字段",
                suggested_repair="让两侧使用同一个问题编号或任务编号字段。",
                node_id=node.id,
            )
    return None
