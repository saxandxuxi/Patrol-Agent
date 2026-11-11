from __future__ import annotations

import copy
import hashlib
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
from ..calculations.semantics import (
    CalculationSourceShape,
    validate_calculation_sources,
)
from ..semantic_aids import canonical_field, detect_evidence
from .analysis_contracts import complete_analysis_answer
from .audit import audit_query_plan
from .capability_registry import CapabilityIssue, CapabilityRegistry
from .comparison_contract import (
    complete_dataset_comparison_dependencies,
    dataset_comparison_contract_issue,
    normalize_dataset_comparison_order,
)
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
    post_calc_has_executable_body,
)
from .fields import FieldReachSet, OutputProjection, PhysicalSchemaView
from .input_bindings import (
    normalize_multi_row_input_bindings,
    query_plan_input_binding_issue,
)
from .periods import dedupe_equivalent_filters, normalize_named_period_window
from .patrol_windows import (
    complete_recent_window_plan,
    missing_recent_window_dependency,
)
from .planner import build_query_plan
from .prompt_facts import build_prompt_facts
from .planning_repair import (
    RepairPatchError,
    RepairTicket,
    patch_signatures,
    repair_issue_from_audit,
    repair_issue_from_capability,
    repair_issue_from_error,
    request_repair_once,
)
from .scalar_sources import complete_scalar_source_selections
from .semantic_operations import (
    GROUP_SHARE,
    answer_operation_catalog,
    semantic_operation_audit_issue,
)
from .shape_contracts import (
    canonical_view,
    normalize_post_calc_shape,
    normalize_segment_projection,
    normalize_top_view,
)
from .spec_mdl import UnsupportedQuerySpec, compile_spec_mdl
from .validator import validate_query_spec
from .values import resolve_value


class QuerySubjectConflict(QuerySpecError):
    def __init__(
        self,
        *,
        initial_subject: str,
        planner_subject: str,
        reason: str = "",
    ) -> None:
        self.initial_subject = str(initial_subject or "").strip()
        self.planner_subject = str(planner_subject or "").strip()
        super().__init__(
            reason
            or (
                f"最终回答主体 {self.planner_subject} "
                f"与已确定主体 {self.initial_subject} 不一致"
            )
        )


def _text_context(value: Any, limit: int = 8000) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:limit]


def normalize_action(action: object) -> tuple[str, tuple[str, ...]]:
    normalized = str(action or "").strip()
    if normalized == "query_data":
        return "query_data", ()
    if normalized == "post_calc":
        return "post_calc", ()
    if normalized in {"analysis", "analyze"}:
        return "post_calc", ("analysis_only",)
    raise QuerySpecError(
        f"invalid_action: {normalized or 'missing'}"
    )


def _audit_capabilities(
    plan: QueryPlan,
    registry: CapabilityRegistry | None,
    reach: FieldReachSet,
) -> dict[str, Any]:
    if registry is None:
        return {}
    subjects = sorted(
        {
            node.spec.subject
            for node in plan.nodes
            if node.spec.subject in registry.snapshot.subjects
        }
    )
    return {
        subject: registry.planner_view(subject, reach).to_prompt_dict()
        for subject in subjects
    }


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    texts: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            term = str(item.get("term") or "").strip()
            if term.startswith("capability_gap:"):
                text = term
            else:
                text = str(
                    item.get("reason")
                    or item.get("message")
                    or term
                    or item.get("capability_gap")
                    or ""
                ).strip()
                if (
                    text
                    and not text.startswith("capability_gap:")
                    and item.get("capability_gap")
                ):
                    text = f"capability_gap:{item['capability_gap']}"
        else:
            text = str(item).strip()
        if text:
            texts.append(text)
    return tuple(texts)


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


def _row_index(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise QuerySpecError("inputs.row_index 必须是从 0 开始的整数")
    if value < 0:
        raise QuerySpecError("inputs.row_index 不能小于 0")
    return value


_METRIC_ALIASES = {
    "sum_length": "sum_disease_length",
    "total_length": "sum_disease_length",
    "avg_length": "average_length",
    "average_length": "avg_disease_length",
    "avg_effective_length": "avg_disease_length",
    "avg_effective_disease_length": "avg_disease_length",
    "average_effective_disease_length": "avg_disease_length",
    "sum_effective_length": "sum_effective_disease_length",
    "total_effective_disease_length": "sum_effective_disease_length",
    "max_effective_disease_length": "max_disease_length",
    "min_effective_disease_length": "min_disease_length",
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
                    row_index=item.row_index,
                )
            )
        specs[index] = QuerySpec(
            **{**spec.__dict__, "input_bindings": tuple(bindings)}
        )


def structured_dependency_binding(
    raw_value: Any,
    target_field: str,
    op: str,
    outputs_by_node: Mapping[str, set[str] | frozenset[str] | tuple[str, ...]],
) -> InputBinding | None:
    """识别 `节点.已声明输出字段`，隐藏字符串协议一律失败关闭。"""
    if not isinstance(raw_value, str):
        return None
    text = raw_value.strip()
    if text.startswith("DEPENDS_ON_"):
        raise QuerySpecError(f"invalid_dependency_value: {text}")
    if "." not in text:
        return None
    node_id, _, source_field = text.rpartition(".")
    node_outputs = outputs_by_node.get(node_id)
    if node_outputs is None:
        return None
    source_field = canonical_field(source_field)
    allowed_fields = {
        canonical_field(field)
        for field in node_outputs
    }
    if not source_field or source_field not in allowed_fields:
        return None
    return InputBinding(
        from_node=node_id,
        from_field=source_field,
        to_field=canonical_field(target_field),
        op=op,
    )


def _declared_outputs_by_node(
    raw_views: list[Any],
) -> dict[str, set[str]]:
    outputs: dict[str, set[str]] = {}
    for index, raw in enumerate(raw_views, 1):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("id") or f"q{index}").strip()
        fields = {
            str(field).rsplit(".", 1)[-1]
            for key in ("metrics", "dimensions", "output_projection")
            for field in (raw.get(key) or ())
            if str(field).strip()
        }
        calculation = raw.get("calculation")
        if isinstance(calculation, dict):
            output = calculation.get("output")
            if isinstance(output, dict) and output.get("name"):
                fields.add(str(output["name"]).rsplit(".", 1)[-1])
        outputs[node_id] = {
            canonical_field(field)
            for field in fields
            if canonical_field(field)
        }
    return outputs


def _invalid_dependency_value(
    raw_value: Any,
    binding: InputBinding | None,
    *,
    node_id: str,
    filter_path: str,
    filter_field: str,
) -> QuerySpecError:
    error = QuerySpecError(
        f"invalid_dependency_value: {str(raw_value or '').strip()}"
    )
    error.dependency_evidence = {
        "node_id": node_id,
        "filter_path": filter_path,
        "filter_field": canonical_field(filter_field),
        "raw_value": str(raw_value or "").strip(),
        **(
            {
                "from_node": binding.from_node,
                "from_field": binding.from_field,
                "to_field": binding.to_field,
                "op": binding.op,
            }
            if binding is not None
            else {}
        ),
    }
    return error


def _normalize_terminal_answer_subject(
    plan: QueryPlan,
    subject: str,
) -> QueryPlan:
    """让不落 SQL 的最终 post_calc 服从语义层确定的回答对象。"""
    if not subject:
        return plan
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
    nodes = []
    changed = False
    for node in plan.nodes:
        if (
            node.id not in consumed
            and node.node_type == "post_calc"
            and node.spec.subject != subject
        ):
            node = replace(
                node,
                spec=replace(node.spec, subject=subject),
            )
            changed = True
        nodes.append(node)
    return replace(plan, nodes=tuple(nodes)) if changed else plan


def _plan_subject_issue(plan: QueryPlan, subject: str) -> str:
    return _plan_subject_conflict(plan, subject)[0]


def _plan_subject_conflict(
    plan: QueryPlan,
    subject: str,
) -> tuple[str, str]:
    if not subject:
        return "", ""
    nodes_by_id = {node.id: node for node in plan.nodes}

    def dependency_ids(node: PlanNode) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *node.depends_on,
                    *(
                        binding.from_node
                        for binding in node.spec.input_bindings
                    ),
                )
            )
        )

    def depends_on_subject(node_id: str, seen: set[str] | None = None) -> bool:
        visited = seen or set()
        if node_id in visited:
            return False
        visited.add(node_id)
        node = nodes_by_id.get(node_id)
        if node is None:
            return False
        return any(
            dependency in nodes_by_id
            and (
                nodes_by_id[dependency].spec.subject == subject
                or depends_on_subject(dependency, visited)
            )
            for dependency in dependency_ids(node)
        )

    consumed = {
        dependency
        for node in plan.nodes
        for dependency in dependency_ids(node)
    }
    terminal_nodes = [
        node for node in plan.nodes if node.id not in consumed
    ]

    def terminal_subject(node: PlanNode) -> str:
        effective_subject = node.spec.subject
        if node.node_type == "post_calc" and node.spec.subject != subject:
            dependency_subjects = {
                nodes_by_id[dependency].spec.subject
                for dependency in node.depends_on
                if dependency in nodes_by_id
            }
            if len(dependency_subjects) == 1:
                effective_subject = next(iter(dependency_subjects))
        return effective_subject

    terminal_subjects = {
        terminal_subject(node)
        for node in terminal_nodes
    }
    mixed_conclusion_exists = any(
        node.node_type == "post_calc"
        and depends_on_subject(node.id)
        and any(
            dependency in nodes_by_id
            and nodes_by_id[dependency].spec.subject != subject
            for dependency in dependency_ids(node)
        )
        for node in terminal_nodes
    )

    def scope_filter_signatures(node: PlanNode) -> set[str]:
        return {
            json.dumps(
                {
                    "field": str(item.field).rsplit(".", 1)[-1],
                    "op": str(item.op),
                    "value": item.storage_value,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            for item in node.spec.filters
        }

    mixed_scope_exists = any(
        scope_filter_signatures(left) & scope_filter_signatures(right)
        for index, left in enumerate(terminal_nodes)
        for right in terminal_nodes[index + 1 :]
        if left.spec.subject != right.spec.subject
    )
    independent_mixed_results = (
        len(terminal_nodes) > 1
        and subject in terminal_subjects
        and len(terminal_subjects) > 1
        and (mixed_conclusion_exists or mixed_scope_exists)
    )
    for node in terminal_nodes:
        effective_subject = terminal_subject(node)
        if effective_subject != subject:
            # 混合明细题的最终节点可以先取巡检查询结果、再下钻病害。
            # 只要它确实依赖已确定主体链上的节点，就不是模型换了用户问题。
            if depends_on_subject(node.id) or independent_mixed_results:
                continue
            return (
                (
                    f"最终回答主体 {effective_subject} "
                    f"与已确定主体 {subject} 不一致"
                ),
                effective_subject,
            )
    return "", ""


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


def _declared_capability_gap_reason(
    terms: tuple[str, ...],
    registry: CapabilityRegistry | None,
    *,
    include_partial: bool = False,
) -> str:
    if registry is None:
        return ""
    reasons: list[str] = []
    allowed_statuses = {"unavailable", "lookup_required"}
    if include_partial:
        allowed_statuses.add("partial")
    for term in terms:
        text = str(term or "").strip()
        prefix = next(
            (
                candidate
                for candidate in ("capability_gap:", "capability_gaps:")
                if text.startswith(candidate)
            ),
            "",
        )
        if not prefix:
            continue
        gap_id = text[len(prefix):].strip()
        raw = registry.snapshot.capability_gaps.get(gap_id)
        if not isinstance(raw, Mapping):
            matched_field = False
            for subject_fields in registry.snapshot.fields.values():
                field = subject_fields.get(gap_id)
                if field is None or field.status not in {
                    "unavailable",
                    "lookup_required",
                }:
                    continue
                reason = "当前数据能力不支持：" + (
                    field.unavailable_reason or "字段能力不可用"
                )
                if reason not in reasons:
                    reasons.append(reason)
                matched_field = True
                break
            if matched_field:
                continue
            allowed = "；".join(
                f"{registered_id}：{reason}"
                for registered_id, registered in (
                    registry.snapshot.capability_gaps.items()
                )
                if isinstance(registered, Mapping)
                and (
                    reason := str(registered.get("reason") or "").strip()
                )
            )
            raise QuerySpecError(
                f"未知能力缺口 id：{gap_id or text}；"
                "只能使用当前能力视图中登记的能力缺口"
                + (f"。可用能力缺口：{allowed}" if allowed else "")
            )
        status = str(raw.get("status") or "unavailable").strip()
        if status not in allowed_statuses:
            continue
        reason = str(raw.get("reason") or "").strip()
        detail = "当前数据能力不支持：" + (
            reason or f"能力缺口 {gap_id} 尚未补齐"
        )
        if detail not in reasons:
            reasons.append(detail)
    return "；".join(reasons)


def _audit_capability_gap_reason(
    verdict: Mapping[str, Any],
    registry: CapabilityRegistry | None,
) -> str:
    gap_ids: list[str] = []
    top_level = str(verdict.get("capability_gap") or "").strip()
    if top_level:
        gap_ids.append(top_level)
    for issue in verdict.get("issues") or []:
        if not isinstance(issue, Mapping):
            continue
        if str(issue.get("code") or "").strip() != "capability_gap":
            continue
        gap_id = str(
            issue.get("capability_gap")
            or issue.get("gap_id")
            or ""
        ).strip()
        if gap_id:
            gap_ids.append(gap_id)
    if not gap_ids:
        return ""
    return _declared_capability_gap_reason(
        tuple(
            f"capability_gap:{gap_id}"
            for gap_id in dict.fromkeys(gap_ids)
        ),
        registry,
        include_partial=True,
    )


def _has_independent_field_support(
    field: str,
    source_evidence: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> bool:
    # A compound phrase can independently request several dimensions.
    # Structured evidence, not disjoint character spans, establishes support.
    return any(
        item is not source_evidence
        and canonical_field(str(item.get("candidate_field") or "")) == field
        for item in evidence
    )


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
    semantic_used_by_spec = {
        spec.id: {
            *[canonical_field(str(item)) for item in spec.dimensions],
            *[
                canonical_field(str(item))
                for item in spec.output_projection
            ],
        }
        for spec in query_specs
    }
    referenced_by_spec = {
        spec.id: {
            *semantic_used_by_spec[spec.id],
            *[
                canonical_field(str(item.field))
                for item in spec.filters
            ],
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
        used_fields = semantic_used_by_spec[spec.id]
        for item in evidence:
            discouraged = {
                str(field) for field in item.get("discouraged_fields") or ()
            }
            used = {
                field
                for field in discouraged.intersection(used_fields)
                if not _has_independent_field_support(
                    field,
                    item,
                    evidence,
                )
            }
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
        for fields in referenced_by_spec.values()
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
    registry: CapabilityRegistry | None = None,
    assist_evidence: tuple[Any, ...] = (),
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
    data_nodes = [
        node for node in plan.nodes if node.node_type == "query_data"
    ]
    explicit_granularity = any(
        str(item.get("candidate_type") or "") == "time_granularity"
        for item in assist_evidence
    )
    default_time_dimension = ""
    if registry is not None:
        trend_contract = (
            registry.snapshot.query_shapes.get("planner_semantics", {})
            .get("answer_operations", {})
            .get("trend", {})
        )
        candidate = str(
            trend_contract.get("default_time_dimension") or ""
            if isinstance(trend_contract, Mapping)
            else ""
        ).strip()
        registered = set(
            registry.snapshot.query_shapes.get("semantic", {})
            .get("group_by", ())
        )
        if candidate and candidate in registered:
            default_time_dimension = candidate
    if len(data_nodes) == 1 and not explicit_granularity and default_time_dimension:
        data = data_nodes[0]
        dimensions = tuple(
            default_time_dimension
            if str(field).rsplit(".", 1)[-1] in {"collection_time", "create_time"}
            else str(field)
            for field in data.spec.dimensions
        )
        if dimensions != data.spec.dimensions:
            order_by = tuple(
                {
                    **item,
                    "field": (
                        default_time_dimension
                        if str(item.get("field") or "").rsplit(".", 1)[-1]
                        in {"collection_time", "create_time"}
                        else item.get("field")
                    ),
                }
                for item in data.spec.order_by
                if isinstance(item, Mapping)
            )
            output_projection = tuple(
                default_time_dimension
                if str(field).rsplit(".", 1)[-1]
                in {"collection_time", "create_time"}
                else str(field)
                for field in data.spec.output_projection
            )
            if not order_by:
                order_by = (
                    {"field": default_time_dimension, "direction": "asc"},
                )
            repaired_data = replace(
                data,
                spec=replace(
                    data.spec,
                    view="trend",
                    dimensions=dimensions,
                    output_projection=output_projection,
                    order_by=order_by,
                ),
            )
            plan = replace(
                plan,
                nodes=tuple(
                    repaired_data if node.id == data.id else node
                    for node in plan.nodes
                ),
            )
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
    if len(data_nodes) == 1:
        data = data_nodes[0]
        label_field = next(iter(data.spec.dimensions), "")
        value_field = next(iter(data.spec.metrics), "")
        if not label_field or not value_field:
            return plan
        nodes = []
        changed = False
        for node in plan.nodes:
            if (
                node.node_type != "post_calc"
                or node.spec.view != "trend"
                or data.id not in node.depends_on
            ):
                nodes.append(node)
                continue
            calculation = node.spec.calculation
            if calculation is not None and calculation.tool != "calculate_series":
                nodes.append(node)
                continue
            calculation = CalculationRequest(
                tool="calculate_series",
                operation="subtract",
                operands=(
                    CalculationOperand(
                        name="labels",
                        source={"node": data.id, "field": label_field},
                    ),
                    CalculationOperand(
                        name="values",
                        source={"node": data.id, "field": value_field},
                    ),
                ),
                output=(
                    calculation.output
                    if calculation is not None
                    else None
                ),
                precision=(
                    calculation.precision
                    if calculation is not None
                    else 6
                ),
            )
            nodes.append(
                replace(
                    node,
                    spec=replace(
                        node.spec,
                        calculation=calculation,
                        required_assertions=tuple(
                            assertion
                            for assertion in node.spec.required_assertions
                            if assertion != "analysis_only"
                        ),
                    ),
                )
            )
            changed = True
        return (
            QueryPlan(
                question=plan.question,
                nodes=tuple(nodes),
                response_mode=plan.response_mode,
            )
            if changed
            else plan
        )
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
                spec=replace(
                    node.spec,
                    calculation=calculation,
                    required_assertions=tuple(
                        assertion
                        for assertion in node.spec.required_assertions
                        if assertion != "analysis_only"
                    ),
                ),
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


def _normalize_group_share_calculation(
    plan: QueryPlan,
    *,
    answer_operation: str = "",
) -> QueryPlan:
    nodes_by_id = {node.id: node for node in plan.nodes}
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
        operands = {item.name: item for item in calculation.operands}
        numerator = operands.get("numerator")
        total = operands.get("total")
        numerator_node_id = (
            str(numerator.source.get("node") or "")
            if numerator is not None and numerator.source
            else ""
        )
        total_node_id = (
            str(total.source.get("node") or "")
            if total is not None and total.source
            else ""
        )
        numerator_source = nodes_by_id.get(numerator_node_id)
        total_source = nodes_by_id.get(total_node_id)
        if (
            answer_operation == GROUP_SHARE
            and numerator_source is not None
            and numerator_source.spec.dimensions
            and (
                total_node_id == numerator_node_id
                or (
                    total_source is not None
                    and not total_source.spec.dimensions
                )
            )
        ):
            nodes.append(
                replace(
                    node,
                    spec=replace(
                        node.spec,
                        calculation=replace(
                            calculation,
                            tool="calculate_columns",
                            operands=tuple(
                                replace(
                                    operand,
                                    source={
                                        key: value
                                        for key, value in operand.source.items()
                                        if key != "row_index"
                                    },
                                )
                                for operand in calculation.operands
                            ),
                        ),
                    ),
                )
            )
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
    raw_draft: dict[str, Any] | None = None,
    draft_capture: dict[str, Any] | None = None,
    intent: str = "",
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
    if raw_draft is None:
        prompt_facts = build_prompt_facts(
            registry,
            planner_view,
        ).as_prompt_values()
        data = chat_json(
            prompts.messages(
                "query_spec",
                question=original,
                kind=kind,
                current_date=datetime.now().strftime("%Y-%m-%d"),
                query_semantics=prompt_facts["query_semantics"],
                answer_operations=prompt_facts["answer_operations"],
                supported_views=prompt_facts["supported_views"],
                capabilities=prompt_facts["capabilities"],
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
    else:
        data = copy.deepcopy(raw_draft)
    if draft_capture is not None and isinstance(data, dict):
        draft_capture.update(
            {
                "draft": copy.deepcopy(data),
                "planner_context": planner_view,
                "assist_evidence": assist_evidence,
            }
        )
    if not isinstance(data, dict):
        raise QuerySpecError("模型没有返回 QuerySpec JSON")
    contract = IntentContract(
        original_question=original,
        kind=kind,
        intent_family=str(data.get("intent_family") or "").strip(),
        final_goal=str(data.get("final_goal") or original).strip(),
        unknown_terms=_string_tuple(data.get("unknown_terms")),
        answer_operation=str(data.get("answer_operation") or "").strip(),
    )
    declared_gap = _declared_capability_gap_reason(
        contract.unknown_terms,
        registry,
    )
    if declared_gap:
        raise QuerySpecUnsupported(declared_gap)
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
        declared_gap = _declared_capability_gap_reason(
            contract.unknown_terms,
            registry,
            include_partial=True,
        )
        if declared_gap:
            raise QuerySpecUnsupported(declared_gap)
        if contract.unknown_terms:
            # 规划器按契约把"当前能力覆盖不到什么"写进了 unknown_terms
            # （例如只注册了 500 米 / 1 公里两种分段粒度），要原样作为
            # 不支持原因返回，而不是暴露"缺少 views"这种内部错误。
            terms = [
                str(item).strip()
                for item in contract.unknown_terms
                if str(item).strip()
            ]
            detail = "；".join(terms)
            if not any(mark in detail for mark in "，。；：,.;:"):
                # 裸词（例如口语病害名）不能原样回显成答案：说清"没匹配上"，
                # 让用户改用台账里登记的名称。
                detail = (
                    f"没有找到与“{'、'.join(terms)}”匹配的可用字段或病害类型，"
                    "请改用台账里登记的名称再问一次"
                )
            raise QuerySpecUnsupported(detail)
        raise QuerySpecError("QuerySpec 缺少 views")
    outputs_by_node = _declared_outputs_by_node(raw_views)
    for index, raw in enumerate(raw_views, 1):
        if not isinstance(raw, dict):
            continue
        spec_id = str(raw.get("id") or f"q{index}").strip()
        selected_subject = str(raw.get("subject") or subject or "disease").strip()
        filters = []
        for filter_index, item in enumerate(raw.get("filters") or []):
            if not isinstance(item, dict):
                raise QuerySpecError("过滤条件必须是对象")
            filter_field = str(item.get("field") or "")
            filter_op = str(item.get("op") or "=")
            raw_value = item.get("raw_value", item.get("value"))
            try:
                dependency_binding = structured_dependency_binding(
                    raw_value,
                    filter_field,
                    filter_op,
                    outputs_by_node,
                )
            except QuerySpecError as exc:
                exc.dependency_evidence = {
                    "node_id": spec_id,
                    "filter_path": (
                        f"/views/{index - 1}/filters/{filter_index}"
                    ),
                    "filter_field": canonical_field(filter_field),
                    "raw_value": str(raw_value or "").strip(),
                }
                raise
            if dependency_binding is not None:
                raise _invalid_dependency_value(
                    raw_value,
                    dependency_binding,
                    node_id=spec_id,
                    filter_path=(
                        f"/views/{index - 1}/filters/{filter_index}"
                    ),
                    filter_field=filter_field,
                )
            filters.append(
                resolve_value(
                    filter_field,
                    filter_op,
                    raw_value,
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
        node_type, action_assertions = normalize_action(
            raw.get("action")
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
                    row_index=_row_index(item.get("row_index")),
                )
            )
        seen_bindings = {
            (
                item.from_node,
                item.from_field,
                item.to_field,
                item.op,
                item.row_index,
            )
            for item in input_bindings
        }
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
            required_assertions=tuple(
                dict.fromkeys(
                    (
                        *_string_tuple(raw.get("required_assertions")),
                        *action_assertions,
                    )
                )
            ),
            question=str(raw.get("question") or original).strip(),
            input_bindings=tuple(input_bindings),
            calculation=calculation,
            comparison_depends_on=_string_tuple(
                raw.get("comparison_depends_on")
            ),
        )
        normalized_spec = normalize_segment_projection(normalized_spec)
        specs.append(
            normalize_top_view(normalized_spec)
            if node_type == "query_data"
            else normalize_post_calc_shape(normalized_spec)
        )
        dependencies[spec_id] = tuple(
            dict.fromkeys(_string_tuple(raw.get("depends_on")))
        )
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
    plan = complete_scalar_source_selections(plan)
    plan = complete_recent_window_plan(plan, registry)
    recent_window_issue = missing_recent_window_dependency(
        plan,
        assist_evidence,
    )
    if recent_window_issue:
        raise QuerySpecError(recent_window_issue)
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
    plan = _complete_trend_analysis(
        plan,
        contract.intent_family,
        registry,
        assist_evidence,
    )
    plan = _complete_trend_dependencies(plan)
    plan = _complete_trend_calculation(plan)
    plan = _complete_scalar_metric_comparisons(plan, registry)
    plan = _normalize_group_share_calculation(
        plan,
        answer_operation=contract.answer_operation,
    )
    plan = normalize_named_period_window(
        plan,
        [
            str(item.get("raw_span") or "")
            for item in assist_evidence
            if item.get("rule_id") == "named_period"
        ],
        datetime.now(),
    )
    plan = dedupe_equivalent_filters(plan)
    plan = normalize_multi_row_input_bindings(plan)
    if registry is not None:
        plan = complete_dataset_comparison_dependencies(plan, registry)
        plan = normalize_dataset_comparison_order(plan)
    plan = _normalize_terminal_answer_subject(plan, subject)
    subject_issue, planner_subject = _plan_subject_conflict(plan, subject)
    if subject_issue:
        raise QuerySubjectConflict(
            initial_subject=subject,
            planner_subject=planner_subject,
            reason=subject_issue,
        )
    intent_issue = _plan_intent_issue(plan, contract.intent_family)
    if intent_issue:
        raise QuerySpecError(intent_issue)
    shaping_issue = _plan_shaping_issue(plan, contract.intent_family)
    if shaping_issue:
        raise QuerySpecError(shaping_issue)
    return complete_analysis_answer(plan, intent)


def _draft_signature(draft: dict[str, Any]) -> str:
    payload = json.dumps(
        draft,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _audit_issue_proposes_change(
    plan: QueryPlan,
    issue: dict[str, Any],
) -> bool:
    changes = issue.get("set")
    if not isinstance(changes, dict) or not changes:
        return True
    node_id = str(issue.get("node_id") or "").strip()
    node = next(
        (item for item in plan.nodes if item.id == node_id),
        None,
    )
    if node is None:
        return True
    values = dict(node.spec.__dict__)
    recognized = {
        "limit",
        "view",
        "metrics",
        "dimensions",
        "order_by",
        "output_projection",
        "required_assertions",
    }
    if any(key not in recognized for key in changes):
        return True
    try:
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
        if "limit" in changes:
            raw_limit = changes["limit"]
            values["limit"] = (
                int(raw_limit) if raw_limit is not None else None
            )
        proposed = normalize_segment_projection(QuerySpec(**values))
        proposed = (
            normalize_top_view(proposed)
            if node.node_type == "query_data"
            else normalize_post_calc_shape(proposed)
        )
    except (TypeError, ValueError):
        return True
    return proposed != node.spec


def _query_plan_compile_error(plan: QueryPlan) -> CapabilityIssue | None:
    for node in plan.nodes:
        if node.node_type != "query_data":
            continue
        try:
            sql = compile_spec_mdl(node.spec)
        except UnsupportedQuerySpec as exc:
            message = str(exc) or f"节点 {node.id} 无法编译"
            return _capability_issue_from_compile_error(
                node.id,
                message,
                getattr(exc, "failure", None),
            )
        if not sql:
            # 确定性编译器没有覆盖并不等于 QuerySpec 非法：
            # 执行入口仍可走受控 LLM MDL-SQL 兜底，并在同一套合同审计后执行。
            # 真正不存在的字段/关系已经由 compile_spec_mdl 抛出
            # UnsupportedQuerySpec 并在上面被拦住。
            continue
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
        if node.node_type != "post_calc":
            if calculation is None:
                continue
            return CapabilityIssue(
                code="calculation_output_invalid",
                original_value=node.id,
                reason="calculation 只能用于 post_calc 节点",
                node_id=node.id,
            )
        if calculation is None and not post_calc_has_executable_body(
            node.spec.required_assertions
        ):
            return CapabilityIssue(
                code="calculation_required",
                original_value=node.id,
                reason=(
                    "post_calc 必须声明 calculation 工具和参数；"
                    "只解释已有值时声明 analysis_only、dataset_comparison 或 "
                    "multi_metric_comparison"
                ),
                suggested_repair=(
                    "需要算术时使用 calculate_scalar、calculate_columns、"
                    "calculate_series 或 calculate_datetime_interval；"
                    "只解释已有值时声明 analysis_only。"
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
                row_index = operand.source.get("row_index")
                if row_index is not None:
                    if calculation.tool not in {
                        "calculate_scalar",
                        "calculate_datetime_interval",
                    }:
                        return CapabilityIssue(
                            code="calculation_operand_missing",
                            original_value=operand.name,
                            reason=(
                                "row_index 只适用于标量或日期差计算"
                                "从有序结果中选择单个值"
                            ),
                            node_id=node.id,
                        )
                    if (
                        isinstance(row_index, bool)
                        or not isinstance(row_index, int)
                        or row_index < 0
                    ):
                        return CapabilityIssue(
                            code="calculation_operand_missing",
                            original_value=operand.name,
                            reason="row_index 必须是从 0 开始的行号",
                            node_id=node.id,
                        )
                    upstream = nodes_by_id.get(source_node)
                    if upstream is None or not upstream.spec.order_by:
                        return CapabilityIssue(
                            code="calculation_operand_missing",
                            original_value=operand.name,
                            reason=(
                                "row_index 只能选择带显式 order_by 的上游结果，"
                                "否则“第一行/第二行”没有确定含义"
                            ),
                            node_id=node.id,
                        )
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
                source_key = (
                    source_node,
                    source_field,
                    row_index,
                )
                duplicate_scalar_source = (
                    calculation.tool == "calculate_scalar"
                    and sum(
                        1
                        for other in calculation.operands
                        if other.source
                        and (
                            str(other.source.get("node") or ""),
                            str(other.source.get("field") or ""),
                            other.source.get("row_index"),
                        )
                        == source_key
                    )
                    > 1
                )
                if (
                    calculation.tool == "calculate_scalar"
                    and not duplicate_scalar_source
                ):
                    upstream = nodes_by_id.get(source_node)
                    metric_fields = (
                        {
                            str(item).rsplit(".", 1)[-1]
                            for item in upstream.spec.metrics
                        }
                        if upstream is not None
                        else set()
                    )
                    source_field_name = source_field.rsplit(".", 1)[-1]
                    if source_field_name not in metric_fields:
                        return CapabilityIssue(
                            code="calculation_type_mismatch",
                            original_value=operand.name,
                            reason=(
                                f"来源 {source_node}.{source_field} "
                                "是分组或展示字段，不是数值指标"
                            ),
                            suggested_repair=(
                                "标量计算只能引用 count/sum/avg 标量视图的数值"
                                "指标；分组结果请给数值指标加 row_index，"
                                "并为上游声明明确的 order_by。"
                            ),
                            node_id=node.id,
                            evidence={
                                "operand": operand.name,
                                "source_node": source_node,
                                "source_field": source_field,
                                "available_metrics": sorted(metric_fields),
                                "suggested_scalar_metric": "count",
                            },
                        )
                    scalar_view = (
                        upstream is not None
                        and upstream.spec.view in {"count", "sum", "avg"}
                        and not upstream.spec.dimensions
                    )
                    if row_index is None and not scalar_view:
                        return CapabilityIssue(
                            code="calculation_type_mismatch",
                            original_value=operand.name,
                            reason=(
                                f"来源 {source_node}.{source_field} 位于分组结果中，"
                                "未提供 row_index，不能当作单个数值"
                            ),
                            suggested_repair=(
                                "增加无分组的 count/sum/avg 标量节点，或给分组数值"
                                "指标指定 row_index 并让上游 order_by 确定行序。"
                            ),
                            node_id=node.id,
                            evidence={
                                "operand": operand.name,
                                "source_node": source_node,
                                "source_field": source_field,
                                "source_view": upstream.spec.view,
                                "source_dimensions": list(upstream.spec.dimensions),
                                "suggested_scalar_metric": "count",
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
                operand.source.get("row_index"),
            )
            for operand in calculation.operands
            if operand.source
        ]
        source_nodes = {source_node for source_node, _, _ in source_keys}
        unused_dependencies = [
            dependency
            for dependency in node.depends_on
            if dependency not in source_nodes
        ]
        if unused_dependencies:
            return CapabilityIssue(
                code="calculation_dependency_unused",
                original_value=calculation.operation,
                reason=(
                    "计算声明了依赖但未消费："
                    + "、".join(unused_dependencies)
                ),
                allowed_values=tuple(node.depends_on),
                suggested_repair=(
                    "让 calculation 的操作数覆盖每个可比较分支；"
                    "若每个分支都需要独立比例，先为每个分支生成计算节点，"
                    "再由一个 post_calc 比较这些结果。"
                ),
                node_id=node.id,
                evidence={
                    "declared_dependencies": list(node.depends_on),
                    "calculation_source_nodes": sorted(source_nodes),
                    "unused_dependencies": unused_dependencies,
                },
            )
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
        if calculation.tool == "calculate_group_change":
            group_change_issue = _group_change_shape_issue(
                node,
                nodes_by_id,
                calculation,
                registry,
            )
            if group_change_issue is not None:
                return group_change_issue
        source_shapes: list[CalculationSourceShape] = []
        operands_by_name = {
            operand.name: operand for operand in calculation.operands
        }
        for role in roles:
            shape = _calculation_source_shape(
                nodes_by_id,
                operands_by_name.get(role),
                registry,
            )
            if shape is not None:
                source_shapes.append(shape)
        semantic_issues = validate_calculation_sources(
            calculation.tool,
            calculation.operation,
            tuple(source_shapes),
            calculation.output.unit if calculation.output else "",
        )
        if semantic_issues:
            issue = semantic_issues[0]
            return CapabilityIssue(
                code=issue.code,
                original_value=calculation.operation,
                reason=issue.message,
                node_id=node.id,
                evidence={
                    **issue.evidence,
                    "tool": calculation.tool,
                    "operation": calculation.operation,
                },
            )
    return None


def _calculation_source_shape(
    nodes_by_id: Mapping[str, PlanNode],
    operand: Any,
    registry: CapabilityRegistry,
) -> CalculationSourceShape | None:
    if operand is None or not operand.source:
        return None
    node = nodes_by_id.get(str(operand.source.get("node") or ""))
    if node is None:
        return None
    field = str(operand.source.get("field") or "")
    metric = registry.snapshot.metric(node.spec.subject, field)
    dimensions = (
        ()
        if operand.source.get("row_index") is not None
        else tuple(node.spec.dimensions)
    )
    if metric is not None:
        result_grain = metric.grain
        identity = metric.identity
        unit = metric.unit
    else:
        output = (
            node.spec.calculation.output
            if node.spec.calculation is not None
            else None
        )
        if output is None or output.name != field:
            return None
        result_grain = node.spec.subject
        identity = output.name
        unit = output.unit
    if dimensions:
        result_grain = "group:" + "|".join(dimensions)
        identity = "group:" + "|".join(dimensions)
    return CalculationSourceShape(
        result_grain=result_grain,
        dimensions=dimensions,
        identity=identity,
        unit=unit,
    )


def _group_change_shape_issue(
    node: PlanNode,
    nodes_by_id: Mapping[str, PlanNode],
    calculation: CalculationRequest,
    registry: CapabilityRegistry,
) -> CapabilityIssue | None:
    if calculation.output is None or not calculation.output.name:
        return CapabilityIssue(
            code="calculation_output_invalid",
            original_value=node.id,
            reason="calculate_group_change 必须声明 output.name",
            node_id=node.id,
        )
    operands = {item.name: item for item in calculation.operands}
    labels = operands.get("labels")
    before = operands.get("before")
    after = operands.get("after")
    if labels is None or before is None or after is None:
        return None
    labels_node_id = str(labels.source.get("node") or "")
    before_node_id = str(before.source.get("node") or "")
    after_node_id = str(after.source.get("node") or "")
    before_node = nodes_by_id.get(before_node_id)
    after_node = nodes_by_id.get(after_node_id)
    labels_node = nodes_by_id.get(labels_node_id)
    if (
        before_node is None
        or after_node is None
        or labels_node is None
        or before_node_id == after_node_id
        or labels_node_id not in {before_node_id, after_node_id}
    ):
        return CapabilityIssue(
            code="calculation_group_shape_mismatch",
            original_value=node.id,
            reason=(
                "分组变化计算必须引用两个不同的前次、后次数据节点，"
                "labels 必须来自其中一个节点。"
            ),
            node_id=node.id,
            evidence={
                "labels_node": labels_node_id,
                "before_node": before_node_id,
                "after_node": after_node_id,
            },
        )
    semantics = registry.snapshot.query_shapes.get(
        "planner_semantics"
    ) or {}
    answer_operations = (
        semantics.get("answer_operations")
        if isinstance(semantics, Mapping)
        else {}
    )
    contract = (
        answer_operations.get("group_change")
        if isinstance(answer_operations, Mapping)
        else {}
    )
    required_views = {
        str(item)
        for item in (
            contract.get("required_dependency_views")
            if isinstance(contract, Mapping)
            else ()
        )
    } or {"distribution", "top_groups"}
    before_spec = before_node.spec
    after_spec = after_node.spec
    labels_field = str(labels.source.get("field") or "").rsplit(".", 1)[-1]
    before_field = str(before.source.get("field") or "").rsplit(".", 1)[-1]
    after_field = str(after.source.get("field") or "").rsplit(".", 1)[-1]
    dimensions_match = (
        len(before_spec.dimensions) == 1
        and before_spec.dimensions == after_spec.dimensions
        and labels_field == before_spec.dimensions[0].rsplit(".", 1)[-1]
    )
    metrics_match = (
        before_field == after_field
        and before_field in {
            item.rsplit(".", 1)[-1] for item in before_spec.metrics
        }
        and after_field in {
            item.rsplit(".", 1)[-1] for item in after_spec.metrics
        }
    )
    views_match = (
        before_spec.view == after_spec.view
        and before_spec.view in required_views
    )
    if (
        before_spec.subject != after_spec.subject
        or before_spec.kind != after_spec.kind
        or not dimensions_match
        or not metrics_match
        or not views_match
    ):
        return CapabilityIssue(
            code="calculation_group_shape_mismatch",
            original_value=node.id,
            reason=(
                "前后两个分组节点必须使用相同主体、类型、分组视图、"
                "同一分组维度和同一指标。"
            ),
            allowed_values=tuple(sorted(required_views)),
            suggested_repair=(
                "保留两个真实批次节点，只改变时间或批次过滤；"
                "让两节点使用相同 dimensions、metrics 和分组 view，"
                "labels/before/after 分别引用共同分组、前次指标、后次指标。"
            ),
            node_id=node.id,
            evidence={
                "before_node": {
                    "id": before_node_id,
                    "subject": before_spec.subject,
                    "kind": before_spec.kind,
                    "view": before_spec.view,
                    "dimensions": list(before_spec.dimensions),
                    "metrics": list(before_spec.metrics),
                },
                "after_node": {
                    "id": after_node_id,
                    "subject": after_spec.subject,
                    "kind": after_spec.kind,
                    "view": after_spec.view,
                    "dimensions": list(after_spec.dimensions),
                    "metrics": list(after_spec.metrics),
                },
                "labels_field": labels_field,
                "before_field": before_field,
                "after_field": after_field,
            },
        )
    return None


def _capability_issue_from_compile_error(
    node_id: str,
    message: str,
    failure: Any = None,
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
    evidence = getattr(failure, "evidence", {}) or {}
    if (
        isinstance(evidence, Mapping)
        and str(evidence.get("reason") or "") == "unknown_metric"
    ):
        metric_id = str(evidence.get("metric") or "")
        return CapabilityIssue(
            code="unknown_metric",
            original_value=metric_id,
            reason=f"节点 {node_id} 使用了未注册的指标 {metric_id}",
            allowed_values=tuple(
                str(item)
                for item in evidence.get("available_metrics") or ()
            ),
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
    return ""


def _audit_failure_message(issues: list[Any]) -> str:
    reasons: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        reason = str(issue.get("reason") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return "独立规划审计发现计划仍不完整：" + "；".join(reasons[:3])
    return "独立规划审计未通过，但没有说明具体缺口；本次查询没有执行。"


def _normalize_plan_shapes(plan: QueryPlan) -> QueryPlan:
    nodes = []
    for node in plan.nodes:
        spec = normalize_segment_projection(node.spec)
        spec = (
            normalize_top_view(spec)
            if node.node_type == "query_data"
            else normalize_post_calc_shape(spec)
        )
        if (
            node.node_type == "post_calc"
            and spec.calculation is not None
            and spec.calculation.tool == "calculate_group_change"
        ):
            spec = replace(
                spec,
                required_assertions=tuple(
                    assertion
                    for assertion in spec.required_assertions
                    if assertion != "dataset_comparison"
                ),
            )
        nodes.append(
            node if spec == node.spec else replace(node, spec=spec)
        )
    return replace(plan, nodes=tuple(nodes))


def repair_audit_issues(
    draft: dict[str, Any],
    issues: list[Any],
    *,
    registry: CapabilityRegistry | None = None,
) -> dict[str, Any]:
    for problem in issues:
        if not isinstance(problem, dict) or not problem.get("code"):
            continue
        issue = repair_issue_from_audit(problem, draft, registry)
        return request_repair_once(
            RepairTicket(
                question=str(
                    draft.get("question")
                    or draft.get("final_goal")
                    or ""
                ),
                draft=draft,
                issue=issue,
            ),
            capabilities={},
            assist_evidence=(),
        )
    return draft


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
    intent: str = "",
) -> QueryPlan:
    feedback = str(planning_feedback or "")
    issues: list[dict[str, Any]] = []
    raw_draft: dict[str, Any] | None = None
    repaired_signatures: set[str] = set()
    repair_rounds = 0
    repair_evidence_signatures: set[str] = set()

    def repair_draft(
        issue: Any,
        draft: dict[str, Any],
        capture: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal repair_rounds
        previous_patch_signatures: set[str] = set()
        while repair_rounds < 2:
            evidence_signature = _draft_signature(
                {
                    "code": issue.code,
                    "path": issue.path,
                    "expected": issue.expected,
                    "actual": issue.actual,
                    "allowed_values": list(issue.allowed_values),
                    "evidence": issue.evidence,
                }
            )
            if evidence_signature in repair_evidence_signatures:
                raise QuerySpecError(
                    f"{issue.code}: QuerySpec 自动修复收到相同失败证据，"
                    "已停止重复执行"
                )
            repair_evidence_signatures.add(evidence_signature)
            repair_rounds += 1
            ticket = RepairTicket(
                question=str(question or "").strip(),
                draft=draft,
                issue=issue,
                attempt=repair_rounds,
                previous_signatures=frozenset(previous_patch_signatures),
            )
            try:
                repaired = request_repair_once(
                    ticket,
                    capabilities=capture.get("planner_context") or {},
                    assist_evidence=capture.get("assist_evidence") or (),
                )
            except LLMError:
                raise
            except RepairPatchError as repair_exc:
                signatures = set(
                    patch_signatures(list(repair_exc.patches))
                )
                if signatures.intersection(previous_patch_signatures):
                    raise QuerySpecError(
                        f"{issue.code}: {repair_exc}；"
                        "自动修复重复提交相同补丁，已停止重复执行"
                    ) from repair_exc
                previous_patch_signatures.update(signatures)
                if repair_rounds >= 2:
                    raise QuerySpecError(
                        f"{issue.code}: 上一轮 QuerySpec 不符合契约，"
                        f"自动修复失败：{issue.reason}；"
                        f"修复阶段：{repair_exc}"
                    ) from repair_exc
                issue = replace(
                    issue,
                    evidence={
                        **issue.evidence,
                        "previous_repair_failure": {
                            "attempt": repair_rounds,
                            "reason": str(repair_exc),
                            "previous_patches": list(repair_exc.patches),
                        },
                    },
                )
                continue
            except Exception as repair_exc:  # noqa: BLE001
                raise QuerySpecError(
                    f"{issue.code}: 上一轮 QuerySpec 不符合契约，"
                    f"自动修复失败：{issue.reason}；"
                    f"修复阶段：{repair_exc}"
                ) from repair_exc
            signature = _draft_signature(repaired)
            if (
                signature == _draft_signature(draft)
                or signature in repaired_signatures
            ):
                raise QuerySpecError(
                    f"{issue.code}: 上一轮 QuerySpec 自动修复"
                    "没有产生新计划，已停止重复执行"
                )
            repaired_signatures.add(signature)
            return repaired
        raise QuerySpecError(
            f"{issue.code}: QuerySpec 已完成两轮受控修复，"
            "仍未通过契约校验"
        )

    for attempt in range(3):
        draft_capture: dict[str, Any] = {}
        next_draft = raw_draft
        raw_draft = None
        try:
            plan = _analyze_once(
                question,
                kind,
                schema,
                reach,
                feedback,
                registry=registry,
                subject=subject,
                intent=intent,
                raw_draft=next_draft,
                draft_capture=draft_capture,
            )
            plan = _normalize_plan_shapes(plan)
        except LLMError as exc:
            if (
                attempt == 0
                and exc.status_code is None
                and exc.category in {"provider_error", "invalid_output"}
            ):
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
        except QuerySubjectConflict:
            raise
        except QuerySpecError as exc:
            draft = draft_capture.get("draft")
            if isinstance(draft, dict) and repair_rounds < 2:
                issue = repair_issue_from_error(
                    exc,
                    draft,
                    registry,
                )
                raw_draft = repair_draft(
                    issue,
                    draft,
                    draft_capture,
                )
                feedback = json.dumps(
                    {
                        "failure_code": issue.code,
                        "reason": issue.reason,
                        "allowed_values": list(issue.allowed_values),
                        "allowed_paths": list(issue.allowed_paths),
                    },
                    ensure_ascii=False,
                )
                continue
            feedback = f"上一轮 QuerySpec 解析失败：{exc}"
            if attempt < 2:
                continue
            raise
        if not audit:
            return plan
        compile_issue = _query_plan_field_issue(plan, registry)
        if compile_issue is None and registry is not None:
            compile_issue = query_plan_input_binding_issue(plan, registry)
        if compile_issue is None and registry is not None:
            compile_issue = dataset_comparison_contract_issue(plan, registry)
        if compile_issue is None:
            compile_issue = _query_plan_derived_metric_issue(plan, registry)
        if compile_issue is None:
            compile_issue = _query_plan_calculation_issue(plan, registry)
        if compile_issue is None:
            compile_issue = _query_plan_compile_error(plan)
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
            draft = draft_capture.get("draft")
            if isinstance(draft, dict) and repair_rounds < 2:
                issue = repair_issue_from_capability(
                    compile_issue,
                    draft,
                )
                raw_draft = repair_draft(
                    issue,
                    draft,
                    draft_capture,
                )
                feedback = json.dumps(
                    {
                        "failure_code": issue.code,
                        "reason": issue.reason,
                        "actual": issue.actual,
                        "allowed_values": list(issue.allowed_values),
                        "allowed_paths": list(issue.allowed_paths),
                    },
                    ensure_ascii=False,
                )
                continue
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
        verdict = audit_query_plan(
            str(question or "").strip(),
            plan,
            capabilities=_audit_capabilities(plan, registry, reach),
            registry=registry,
        )
        audit_gap = _audit_capability_gap_reason(verdict, registry)
        if audit_gap:
            raise QuerySpecUnsupported(audit_gap)
        operation_issue = semantic_operation_audit_issue(
            plan,
            verdict,
            registry,
        )
        if operation_issue:
            feedback = json.dumps(
                {
                    **operation_issue.to_feedback(),
                    "independent_audit_operation": str(
                        verdict.get("answer_operation") or ""
                    ),
                },
                ensure_ascii=False,
                default=str,
            )
            draft = draft_capture.get("draft")
            if isinstance(draft, dict) and repair_rounds < 2:
                issue = repair_issue_from_capability(
                    operation_issue,
                    draft,
                )
                raw_draft = repair_draft(
                    issue,
                    draft,
                    draft_capture,
                )
                feedback = json.dumps(
                    {
                        "failure_code": issue.code,
                        "reason": issue.reason,
                        "actual": issue.actual,
                        "allowed_values": list(issue.allowed_values),
                        "allowed_paths": list(issue.allowed_paths),
                    },
                    ensure_ascii=False,
                )
                continue
            if attempt < 2:
                continue
            raise QuerySpecError(
                "独立语义审计认定这一步是实体集合比较，"
                "但计划没有提供可执行的集合差集结构："
                + operation_issue.reason
            )
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
            if _audit_issue_proposes_change(plan, issue)
        ]
        if not effective_issues:
            return plan
        verdict = {**verdict, "issues": effective_issues}
        patch_suggestions = [
            item
            for item in effective_issues
            if isinstance(item.get("set"), dict) and item["set"]
        ]
        draft = draft_capture.get("draft")
        if (
            patch_suggestions
            and isinstance(draft, dict)
            and repair_rounds < 2
        ):
            issue = repair_issue_from_audit(
                patch_suggestions[0],
                draft,
                registry,
            )
            raw_draft = repair_draft(
                issue,
                draft,
                draft_capture,
            )
            feedback = json.dumps(
                {
                    "failure_code": issue.code,
                    "reason": issue.reason,
                    "expected": issue.expected,
                    "actual": issue.actual,
                    "allowed_paths": list(issue.allowed_paths),
                },
                ensure_ascii=False,
            )
            continue
        issues = verdict.get("issues") or []
        feedback = (
            "上一轮 QuerySpec 未通过独立规划审计。只在用户原问题和现有字段、"
            "字典允许的范围内补齐或修正；不得猜测时间、巡检批次、比较对象、"
            "字段或枚举。审计问题："
            + json.dumps(issues, ensure_ascii=False, default=str)
        )
    ambiguity = _ambiguity_message(issues)
    if ambiguity:
        raise QuerySpecAmbiguity(ambiguity)
    raise QuerySpecError(_audit_failure_message(issues))
