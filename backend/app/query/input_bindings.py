from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .capability_registry import CapabilityIssue, CapabilityRegistry
from .contracts import InputBinding, QueryPlan, QuerySpec


def _field_id(value: Any) -> str:
    return str(value).rsplit(".", 1)[-1].strip()


def _registered_field_equivalences(
    registry: CapabilityRegistry,
) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()
    raw_items = registry.snapshot.aliases.get("field_equivalences") or ()
    if not isinstance(raw_items, (list, tuple)):
        return pairs
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        left = _field_id(item.get("left"))
        right = _field_id(item.get("right"))
        if left and right and left != right:
            pairs.add(frozenset({left, right}))
    return pairs


def _outputs(spec: QuerySpec) -> set[str]:
    return {
        _field_id(item)
        for item in (
            *spec.dimensions,
            *spec.metrics,
            *spec.output_projection,
        )
    }


def _source_may_return_multiple_rows(spec: QuerySpec) -> bool:
    if spec.view in {"count", "sum", "avg"} and not spec.dimensions:
        return False
    return spec.limit is None or int(spec.limit) > 1


def _data_type_family(data_type: str) -> str:
    value = str(data_type or "").lower().split("(", 1)[0].strip()
    if any(item in value for item in ("time", "date", "year")):
        return "temporal"
    if any(item in value for item in ("char", "text", "enum", "calculated")):
        return "text"
    if any(
        item in value
        for item in (
            "int",
            "decimal",
            "numeric",
            "double",
            "float",
            "real",
            "bit",
        )
    ):
        return "number"
    return ""


def _compatibility_issue(
    registry: CapabilityRegistry,
    source_subject: str,
    source_field: str,
    target_subject: str,
    target_field: str,
) -> str:
    equivalent = (
        source_field == target_field
        or frozenset({source_field, target_field})
        in _registered_field_equivalences(registry)
    )
    if not equivalent:
        return (
            f"上游字段 {source_subject}.{source_field} 与下游字段 "
            f"{target_subject}.{target_field} 没有登记为等价字段，"
            "不能用字段类型相同代替字段语义相同"
        )
    source = registry.snapshot.fields.get(source_subject, {}).get(source_field)
    target = registry.snapshot.fields.get(target_subject, {}).get(target_field)
    if source is None or target is None:
        return ""
    source_is_time = "time" in source.roles
    target_is_time = "time" in target.roles
    if source_is_time != target_is_time:
        return (
            f"上游字段 {source_subject}.{source_field} 与下游字段 "
            f"{target_subject}.{target_field} 的语义类型不兼容"
        )
    source_family = _data_type_family(source.data_type)
    target_family = _data_type_family(target.data_type)
    if (
        source_family
        and target_family
        and source_family != target_family
    ):
        return (
            f"上游字段 {source_subject}.{source_field} 的类型 "
            f"{source.data_type} 不能绑定到下游字段 "
            f"{target_subject}.{target_field} 的类型 {target.data_type}"
        )
    return ""


def _binding_issue(
    binding: InputBinding,
    source: QuerySpec,
    target: QuerySpec,
    registry: CapabilityRegistry,
) -> CapabilityIssue | None:
    source_field = _field_id(binding.from_field)
    target_field = _field_id(binding.to_field)
    if not source_field or not target_field:
        return CapabilityIssue(
            code="input_binding_incomplete",
            original_value=str(binding),
            reason="输入绑定必须同时声明 from_field 和 to_field",
            suggested_repair="补齐来源字段和目标字段，不要留下空字段名。",
            node_id=target.id,
        )
    if source_field not in _outputs(source):
        return CapabilityIssue(
            code="input_field_not_produced",
            original_value=source_field,
            reason=(
                f"输入依赖 {binding.from_node}.{source_field} "
                "没有被上游节点投影或聚合产出"
            ),
            allowed_values=tuple(sorted(_outputs(source))),
            suggested_repair=(
                "把真实来源字段加入上游 dimensions/output_projection，"
                "或把 from_field 改成上游确实产出的字段。"
            ),
            node_id=target.id,
            evidence={
                "from_node": binding.from_node,
                "from_field": source_field,
            },
        )
    if binding.row_index is not None:
        if binding.row_index < 0:
            return CapabilityIssue(
                code="input_row_index_invalid",
                original_value=str(binding.row_index),
                reason="row_index 必须是从 0 开始的行号",
                suggested_repair="使用 0 表示第一行，1 表示第二行。",
                node_id=target.id,
            )
        if not source.order_by:
            return CapabilityIssue(
                code="input_row_order_missing",
                original_value=str(binding.from_node),
                reason="row_index 只能读取带显式 order_by 的上游结果",
                suggested_repair="给上游节点声明确定行序的 order_by。",
                node_id=target.id,
            )
        if source.limit is not None and binding.row_index >= source.limit:
            return CapabilityIssue(
                code="input_row_index_out_of_range",
                original_value=str(binding.row_index),
                reason=(
                    f"上游 limit={source.limit}，不存在从 0 开始计数的第 "
                    f"{binding.row_index + 1} 行"
                ),
                suggested_repair="调整 row_index 或上游 limit。",
                node_id=target.id,
            )
    elif (
        binding.op not in {"in", "between"}
        and _source_may_return_multiple_rows(source)
    ):
        return CapabilityIssue(
            code="input_single_value_requires_row_index",
            original_value=f"{binding.from_node}.{source_field}",
            reason=(
                f"输入依赖 {binding.from_node}.{source_field} 可能返回多行，"
                f"但目标条件 {binding.op} 只接收一个值"
            ),
            allowed_values=("row_index", "in", "between"),
            suggested_repair=(
                "只消费一个确定行时声明 row_index，并让上游有明确 order_by；"
                "消费同一字段的整组值时把 op 改成 in 或 between。"
            ),
            node_id=target.id,
            evidence={
                "from_node": binding.from_node,
                "from_field": source_field,
                "source_view": source.view,
                "source_limit": source.limit,
                "source_dimensions": list(source.dimensions),
            },
        )
    incompatibility = _compatibility_issue(
        registry,
        source.subject,
        source_field,
        target.subject,
        target_field,
    )
    if incompatibility:
        return CapabilityIssue(
            code="input_field_incompatible",
            original_value=f"{source_field}->{target_field}",
            reason=incompatibility,
            allowed_values=(source_field,),
            suggested_repair=(
                "让上游产出下游需要的真实字段；显示值和存储值只能使用"
                "已登记的等价字段对。"
            ),
            node_id=target.id,
            evidence={
                "from_subject": source.subject,
                "from_field": source_field,
                "to_subject": target.subject,
                "to_field": target_field,
            },
        )
    return None


def query_plan_input_binding_issue(
    plan: QueryPlan,
    registry: CapabilityRegistry,
) -> CapabilityIssue | None:
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        if node.node_type == "query_data" and node.depends_on:
            bound_sources = {
                binding.from_node for binding in node.spec.input_bindings
            }
            missing_sources = [
                dependency
                for dependency in node.depends_on
                if dependency not in bound_sources
            ]
            if missing_sources:
                return CapabilityIssue(
                    code="missing_input_binding",
                    original_value=node.id,
                    reason=(
                        f"节点 {node.id} 声明依赖 "
                        f"{'、'.join(missing_sources)}，但没有输入绑定；"
                        "查询节点无法仅凭 depends_on 消费上游结果"
                    ),
                    allowed_values=tuple(node.depends_on),
                    suggested_repair=(
                        "如果这一步是在上游结果上继续派生，改用 post_calc "
                        "并按计算工具声明操作数；如果只是过滤查询，"
                        "为每个依赖声明 from_node、from_field、to_field 和 op。"
                    ),
                    node_id=node.id,
                    evidence={
                        "depends_on": list(node.depends_on),
                        "input_bindings": [
                            {
                                "from_node": item.from_node,
                                "from_field": item.from_field,
                                "to_field": item.to_field,
                                "op": item.op,
                            }
                            for item in node.spec.input_bindings
                        ],
                        "missing_sources": missing_sources,
                    },
                )
        bindings_by_source: dict[str, list[InputBinding]] = {}
        for binding in node.spec.input_bindings:
            bindings_by_source.setdefault(binding.from_node, []).append(binding)
        for source_id, bindings in bindings_by_source.items():
            source_node = nodes_by_id.get(source_id)
            if (
                source_node is None
                or node.node_type != "query_data"
                or len(bindings) < 2
                or len(source_node.spec.dimensions) < 2
                or not _source_may_return_multiple_rows(source_node.spec)
            ):
                continue
            row_indices = {item.row_index for item in bindings}
            if len(row_indices) == 1 and None not in row_indices:
                continue
            return CapabilityIssue(
                code="input_composite_rows_unsupported",
                original_value=source_id,
                reason=(
                    f"节点 {node.id} 同时绑定 {source_id} 的多个分组字段，"
                    "但普通过滤条件只能把各字段展开成独立集合，"
                    "会丢失这些字段原本的同一行配对关系"
                ),
                allowed_values=tuple(
                    item.from_field for item in bindings
                ),
                suggested_repair=(
                    "不要把多行组合拆成多个 in 过滤；改为一个按这些字段分组"
                    "的查询，再由 post_calc 消费完整分组结果。若只消费确定的一行，"
                    "给同一来源的全部绑定声明相同 row_index。"
                ),
                node_id=node.id,
                evidence={
                    "from_node": source_id,
                    "source_dimensions": list(source_node.spec.dimensions),
                    "bound_fields": [
                        {
                            "from_field": item.from_field,
                            "to_field": item.to_field,
                            "op": item.op,
                            "row_index": item.row_index,
                        }
                        for item in bindings
                    ],
                },
            )
        for binding in node.spec.input_bindings:
            source = nodes_by_id.get(binding.from_node)
            if source is None:
                return CapabilityIssue(
                    code="input_node_unknown",
                    original_value=binding.from_node,
                    reason=f"输入绑定引用了不存在的上游节点 {binding.from_node}",
                    allowed_values=tuple(nodes_by_id),
                    suggested_repair="把 from_node 改成计划中真实存在的节点 id。",
                    node_id=node.id,
                )
            issue = _binding_issue(
                binding,
                source.spec,
                node.spec,
                registry,
            )
            if issue is not None:
                return issue
    return None
