from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from ..services import (
    field_grounding,
    preset_lib,
    schema_catalog,
    sql_engine,
    templatizer,
    wren_engine,
)
from ..services.llm import LLMError
from ..services.logging_service import console_json
from ..services.route_resolver import QueryPlanError
from .analysis import QuerySubjectConflict, analyze_single_turn
from .capability_registry import CapabilityRegistry, resolve_subject
from .compiler import CompiledQuery, compile_query
from .confirmations import (
    ConfirmationTicket,
    consume_confirmation,
    issue_confirmation,
)
from .contracts import (
    AnswerPackage,
    ExecutionObservation,
    QueryPlan,
    QuerySpecAmbiguity,
    QuerySpecError,
    QuerySpecUnsupported,
    UserDecision,
    ValueBinding,
    immutable_scope_hash,
    plan_scope_hash,
    scope_hash,
)
from .empty_results import (
    aggregate_result_is_empty as _aggregate_is_empty,
    is_outside_scope_empty_answerable,
    outside_scope_empty_answer,
    zero_count_observation,
)
from .executor import execute_compiled
from .fields import FieldReachHit, FieldReachSet, PhysicalSchemaView
from .llm_failures import describe_llm_failure
from .mdl_sql import (
    MdlSqlPipelineError,
    compile_queryspec_to_physical,
    failure_message as mdl_failure_message,
)
from .patrol_batches import (
    PatrolBatchResolutionError,
    describe_batch_lookup_evidence,
    resolve_entity_comparison_batches,
    validate_explicit_patrol_batches,
)
from .patrol_windows import complete_recent_window_groups
from .preset_bindings import apply_overrides, values_from_spec
from .presentation.rows import (
    empty_metric_note as _empty_metric_note,
    format_display_value as _format_display_value,
    format_row_text as _format_row_text,
    missing_value_labels as _missing_value_labels,
)
from .presentation.boundaries import (
    explicit_range_note,
    partial_coverage_scope_notice,
)
from .presentation.conditions import observation_group_labels
from .presentation.enumerations import (
    complete_explicit_enum_groups,
    missing_explicit_top_group_labels,
)
from .presentation.units import (
    metric_unit as _metric_unit,
    registered_units as _resolve_registered_units,
)
from .review import ReviewSuspicion, issue_review_gate, serialize_query_spec
from .result_projection import result_field_map
from .scope_facts import (
    INSIDE,
    OUTSIDE,
    PARTIAL,
    ScopeFacts,
    empty_scope_facts,
    coverage_facts,
)
from .template_sources import (
    AutoTemplateProvider,
    BuiltinTemplateProvider,
    PresetTemplateProvider,
    VerifiedExampleTemplateProvider,
)
from .spec_mdl import UnsupportedQuerySpec, compile_spec_mdl
from .templates import CandidateSet, TemplateCandidate, resolve_templates
from .time_coverage import time_window_coverage
from .values import _enum_location, resolve_value as resolve_binding_value
from .verifier import verify_result


_simple_spec_mdl = compile_spec_mdl


@dataclass(frozen=True)
class TurnStart:
    plan: QueryPlan
    candidate_set: CandidateSet | None = None
    confirmation_ticket: ConfirmationTicket | None = None
    answer: AnswerPackage | None = None


@dataclass(frozen=True)
class _PendingTurn:
    plan: QueryPlan
    candidates: CandidateSet
    schema: PhysicalSchemaView
    registry: CapabilityRegistry | None = None
    # 确认后仍要能跑"计算节点失败 → 重排一次"的修复，所以确认卡上必须
    # 带着原始问题、库类型、字段触达和模板来源。以前这几个字段没存，
    # 用户点确认卡走的路径完全没有修复机会（计划只在执行期暴雷）。
    question: str = ""
    kind: str = "mysql"
    reach: FieldReachSet | None = None
    providers: tuple[Any, ...] = ()


class _EmptyDependencyInput(UnsupportedQuerySpec):
    def __init__(self, node_id: str, field: str):
        self.node_id = str(node_id)
        self.field = str(field)
        super().__init__(
            f"输入依赖 {self.node_id}.{self.field} 没有可传递的值"
        )


_PENDING: dict[str, _PendingTurn] = {}


def _default_schema(kind: str) -> PhysicalSchemaView:
    catalog = schema_catalog.field_catalog(kind)
    columns: set[tuple[str, str]] = set()
    for table in catalog.get("tables") or []:
        table_name = str(table.get("table") or "")
        for column in table.get("columns") or []:
            column_name = str(column.get("name") or "")
            if table_name and column_name:
                columns.add((table_name, column_name))
    snapshot = json.dumps(sorted(columns), ensure_ascii=False)[:4096]
    return PhysicalSchemaView(columns=frozenset(columns), snapshot=snapshot)


def _field_reach_from_ground(grounded: dict[str, Any] | None) -> FieldReachSet:
    hits = []
    for item in (grounded or {}).get("columns") or []:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "")
        column = str(item.get("column") or "")
        if not table or not column:
            continue
        matched = "；".join(str(value) for value in item.get("hits") or ())
        hits.append(
            FieldReachHit(
                table=table,
                column=column,
                matched_text=matched or column,
                source="field_grounding",
                score=float(item.get("score") or 0),
            )
        )
    return FieldReachSet(hits=tuple(hits))


def _default_template_executor(
    candidate: TemplateCandidate,
    overrides: dict[str, Any],
    spec: Any,
) -> str:
    override_values = {key: str(value) for key, value in overrides.items()}
    if candidate.contract.adapter_id == "preset":
        query = preset_lib.query_by_id(candidate.contract.native_id)
        if not query:
            raise ValueError(
                f"查询模板不存在：{candidate.contract.native_id}"
            )
        values = values_from_spec(spec)
        apply_overrides(values, override_values)
        sql, reason = preset_lib.bind(query, values)
        if not sql:
            raise ValueError(
                f"模板 {candidate.contract.native_id} 无法绑定参数：{reason}"
            )
        return sql
    if candidate.contract.adapter_id == "builtin":
        return _compile_queryspec_to_physical(spec)
    if candidate.contract.adapter_id == "auto":
        template = next(
            (
                item
                for item in templatizer.list_templates()
                if str(item.get("id") or "") == candidate.contract.native_id
            ),
            None,
        )
        if not template:
            return ""
        values = override_values
        for name, parameter in (template.get("params") or {}).items():
            if not isinstance(parameter, dict):
                continue
            declare = str(parameter.get("declare") or "")
            column = str(parameter.get("column") or "")
            for binding in spec.filters:
                if binding.field == declare or binding.field == column:
                    values[name] = str(binding.storage_value)
                    break
                if declare == "route" and binding.field == "route_name":
                    values[name] = str(binding.storage_value)
                    break
        sql, _reason = templatizer.bind(template, values)
        return sql or ""
    raise RuntimeError(
        f"template compiler adapter not configured: {candidate.contract.adapter_id}"
    )


def _default_wren_executor(spec: Any, *, plan_id: str = "") -> str:
    return compile_queryspec_to_physical(spec, plan_id=plan_id)


def _compile_queryspec_to_physical(spec: Any) -> str:
    return compile_queryspec_to_physical(spec)


def _default_execute_sql(
    kind: str,
    sql: str,
    question: str = "",
) -> dict[str, Any]:
    return sql_engine.execute_sql(kind, sql, question=question)


def _default_post_calc_executor(
    task: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from ..calculations.post_calc import execute_post_calc

    return execute_post_calc(task, results)


def _data_answer_text(section: dict[str, Any]) -> str:
    view = str(section.get("view") or "")
    section_type = str(section.get("type") or "")
    title = str(section.get("title") or "").strip()
    prefix = f"“{title}”" if title else "本次查询"
    section_subject = _section_subject(section)
    display_units = section.get("display_units")
    if not isinstance(display_units, dict):
        display_units = _resolve_registered_units(section.get("spec"))
    missing_labels = section.get("missing_value_labels")
    if not isinstance(missing_labels, dict):
        missing_labels = {}
    if str(section.get("status") or "accepted") != "accepted":
        return str(section.get("answer") or "").strip()
    rows_fetched = int(
        section.get("rows_fetched")
        or len(section.get("rows") or section.get("preview_rows") or [])
    )
    total_count = section.get("total_count")
    if section_type == "post_calc":
        return str(section.get("answer") or "").strip()
    if view in {"list", "detail", "top_records"}:
        preview = [
            row
            for row in (section.get("rows") or section.get("preview_rows") or ())
            if isinstance(row, dict)
        ]
        inherited = _inherited_note(section)
        suffix = f"；{inherited}" if inherited else ""
        if rows_fetched == 0:
            return f"{prefix}没有匹配记录（0 条）{suffix}"
        # 只有一两条记录时，用户要的是答案本身，不是“请去工作台看”。
        if 0 < len(preview) <= 3:
            detail = "；".join(
                _format_row_text(
                    row,
                    subject=section_subject,
                    units=display_units,
                    missing_labels=missing_labels,
                )
                for row in preview
            )
            if total_count is not None and int(total_count) > len(preview):
                return (
                    f"{prefix}共 {int(total_count)} 条，当前展示前 {len(preview)} 条："
                    f"{detail}；完整明细请在结果工作台查看{suffix}。"
                )
            # 没有总数、也没有被截断时，这几条就是全部：说清楚，别让用户
            # 怀疑“是不是还有没列出来的”。
            if total_count is None and not section.get("truncated"):
                return f"{prefix}共 {rows_fetched} 条（已全部列出）：{detail}{suffix}"
            return f"{prefix}{detail}{suffix}"
        if total_count is not None:
            return (
                f"{prefix}共 {int(total_count)} 条，当前展示前 {rows_fetched} 条，"
                f"完整明细请在结果工作台查看{suffix}。"
            )
        return (
            f"{prefix}返回 {rows_fetched} 条，明细已生成，"
            f"请在结果工作台查看{suffix}。"
        )
    if view in {"distribution", "trend", "top_groups"}:
        filled = [str(item) for item in (section.get("enum_filled_groups") or ())]
        result_rows = [
            row
            for row in (section.get("rows") or section.get("preview_rows") or ())
            if isinstance(row, dict)
        ]
        shown = len(result_rows) or rows_fetched
        digest_limit = (
            len(result_rows)
            if view == "top_groups" and len(result_rows) <= 10
            else 3
        )
        digest = _grouped_digest(
            result_rows,
            limit=digest_limit,
            subject=section_subject,
            units=display_units,
            missing_labels=missing_labels,
        )
        if shown == 0:
            text = f"{prefix}在查询范围内没有匹配记录（0 行）"
        else:
            text = f"{prefix}查询完成，共 {shown} 行"
            if digest:
                # 结果很少时要说明“这就是全部”，否则用户分不清是完整清单
                # 还是只看了前几名。
                if shown <= 3:
                    text = f"{text}（已全部列出）：{digest}"
                else:
                    text = f"{text}；按排序前列：{digest}"
        spec = section.get("spec")
        requested_top = (
            spec.get("limit")
            if isinstance(spec, dict)
            else getattr(spec, "limit", None)
        )
        insufficient_top_note = (
            f"仅命中 {shown} 个分组，少于请求的 Top {requested_top}"
            if (
                view == "top_groups"
                and isinstance(requested_top, int)
                and 0 < shown < requested_top
            )
            else ""
        )
        notes = [
            _inherited_note(section),
            str(section.get("coverage_note") or ""),
            (
                "以下请求分组在现有覆盖范围内没有匹配记录："
                + "、".join(
                    str(item)
                    for item in (
                        section.get("missing_requested_groups") or ()
                    )
                )
                if section.get("missing_requested_groups")
                else ""
            ),
            (
                "范围内没有" + "、".join(filled) + "的记录，已按 0 计入"
                if filled
                else ""
            ),
            (
                "在现有记录覆盖到的重叠区间内未观察到匹配记录"
                if shown == 0
                and section.get("coverage_verdict") == PARTIAL
                else ""
            ),
            _empty_metric_note(
                result_rows,
                missing_labels=missing_labels,
            ),
            insufficient_top_note,
            "完整表格请在结果工作台查看",
        ]
        suffix = "；".join(item for item in notes if item)
        return f"{text}；{suffix}" if suffix else text
    return str(section.get("answer") or "").strip()


def _section_subject(section: dict[str, Any]) -> str:
    subject = str(section.get("subject") or "").strip()
    if subject:
        return subject
    spec = section.get("spec")
    return (
        str(spec.get("subject") or "").strip()
        if isinstance(spec, dict)
        else ""
    )


def _grouped_digest(
    rows: list[dict[str, Any]],
    limit: int = 3,
    *,
    subject: str = "",
    units: dict[str, str] | None = None,
    missing_labels: dict[str, str] | None = None,
) -> str:
    """分组结果先给一行确定性的摘要，别只留一句“请去工作台看”。

    行数几十上百时，用户需要的是“哪几个分组最突出”。SQL 已经按指标排好序，
    这里只做展示，不重新计算，也不改变任何数值。
    """
    lines = [
        _format_row_text(
            row,
            subject=subject,
            units=units,
            missing_labels=missing_labels,
        )
        for row in rows[:limit]
    ]
    return "；".join(item for item in lines if item)


def _inherited_note(section: dict[str, Any]) -> str:
    """依赖查询沿用了上一节点的条件时，回答里必须写明，不能让用户猜。"""
    inherited = "、".join(
        f"{item.get('label') or ''} {item.get('value') or ''}".strip()
        for item in (section.get("inherited_filters") or ())
        if isinstance(item, dict)
    )
    if not inherited:
        return ""
    return f"本次结果沿用了上一节点筛选出的条件：{inherited}"


def _inherited_filter_columns(
    spec: Any,
    registry: CapabilityRegistry | None,
) -> list[dict[str, str]]:
    """依赖查询把上一节点的结果变成了过滤条件，这些条件必须在结果里看得见。

    否则用户看到“K0+000~K0+500 共 304 处”，却不知道这是哪条路的数据。
    """
    columns: list[dict[str, str]] = []
    for item in spec.filters:
        if str(getattr(item, "source", "")) != "previous_result":
            continue
        field = str(item.field).rsplit(".", 1)[-1]
        label = field
        if registry is not None:
            field_meta = registry.snapshot.fields.get(spec.subject, {}).get(field)
            if field_meta is not None and field_meta.label:
                label = str(field_meta.label)
        value = item.display_value or item.storage_value
        if isinstance(value, (list, tuple)):
            value = "、".join(str(entry) for entry in value)
        columns.append({"field": field, "label": str(label), "value": str(value)})
    return columns


def assemble_answer(
    sections: list[dict[str, Any]],
    response_mode: str = "data",
) -> AnswerPackage:
    text_sections = sections
    has_failed_sections = any(
        str(section.get("status") or "accepted") != "accepted"
        for section in sections
    )
    if response_mode == "analysis" and not has_failed_sections:
        conclusions = [
            section
            for section in sections
            if str(section.get("type") or "") in {"analysis", "post_calc"}
        ]
        if conclusions:
            text_sections = [
                section
                for section in sections
                if section in conclusions
                or (
                    str(section.get("type") or "") == "data"
                    and "analysis_context"
                    in (
                        section.get("spec", {}).get(
                            "required_assertions",
                            (),
                        )
                        if isinstance(section.get("spec"), dict)
                        else ()
                    )
                )
            ]
    texts: list[str] = []
    for section in text_sections:
        # 分析模式下数据节点没有结论可写时，也不能把明细行原样倒出来；
        # 数据节点统一走数据回答规则（只给摘要并指路结果工作台）。
        text = (
            str(section.get("answer") or "").strip()
            if response_mode != "data"
            and str(section.get("type") or "") == "post_calc"
            else _data_answer_text(section)
        )
        if text and text not in texts:
            texts.append(text)
    if not texts:
        return AnswerPackage("查询完成，但没有可展示的数据。")
    text = "；".join(texts)
    coverage_warning = partial_coverage_scope_notice(
        section.get("coverage_verdict")
        for section in sections
    )
    if coverage_warning:
        text = f"{coverage_warning}；{text}"
    coverage_details = list(
        dict.fromkeys(
            str(section.get("coverage_detail") or "").strip()
            for section in sections
            if str(section.get("coverage_verdict") or "")
            in {PARTIAL, OUTSIDE}
            and str(section.get("coverage_detail") or "").strip()
        )
    )
    uncovered_details = [
        detail for detail in coverage_details if detail not in text
    ]
    if uncovered_details:
        text = f"{text}；覆盖边界：{'；'.join(uncovered_details)}"
    if text[-1:] not in "。！？.!?；;":
        text += "。"
    return AnswerPackage(text, tuple(sections))


def _terminal_sections(
    plan: QueryPlan,
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dependency_ids = {
        dependency
        for node in plan.nodes
        for dependency in node.depends_on
    }
    terminal = [
        section
        for section in sections
        if str(section.get("status") or "accepted") != "accepted"
        or str(section.get("node_id") or "") not in dependency_ids
    ]
    return terminal or sections


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _hashable(item))
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_hashable(item) for item in value)
    return value


def _scope_key(spec: Any) -> tuple[Any, ...]:
    return (
        spec.kind,
        spec.subject,
        tuple(
            (item.field, item.op, _hashable(item.storage_value))
            for item in spec.filters
        ),
        tuple(spec.dimensions),
    )


def _dependency_needs_dictionary(field: str, spec: Any) -> bool:
    """依赖传值是否需要先过字典解析。

    上游结果行给的是展示视图里的值（路线中文名、枚举中文标签），而过滤条件
    要的是对应字段的存储值。`routes_id` 要转成路线 id，已登记枚举字段要
    转成 code；`route_name` 也要先校验路线是否存在，但目标字段仍是
    `route_name` 时必须保留展示值。这样显示值和存储值不会互相污染。
    """
    name = str(field).rsplit(".", 1)[-1]
    if name in {"route_name", "routes_id"}:
        return True
    registry = CapabilityRegistry.load(
        str(getattr(spec, "kind", "mysql") or "mysql")
    )
    location = _enum_location(
        registry,
        str(getattr(spec, "subject", "") or ""),
        name,
    )
    return location in registry.snapshot.enums


def _resolve_dependency_binding(binding: Any, values: list[Any], spec: Any) -> Any:
    try:
        return resolve_binding_value(
            str(binding.to_field),
            "in",
            list(values),
            str(getattr(spec, "question", "") or ""),
            str(getattr(spec, "kind", "mysql") or "mysql"),
            subject=str(getattr(spec, "subject", "") or ""),
        )
    except (QueryPlanError, QuerySpecError, QuerySpecAmbiguity) as exc:
        raise UnsupportedQuerySpec(
            "上一步结果里的取值“"
            + "、".join(str(item) for item in values)
            + f"”无法解析成可执行值（{binding.to_field}）：{exc}"
        ) from exc


def _materialize_inputs(
    spec: Any,
    accepted_observations: dict[str, Any],
    accepted_specs: dict[str, Any],
) -> Any:
    if not spec.input_bindings:
        return spec
    filters = list(spec.filters)
    for binding in spec.input_bindings:
        observation = accepted_observations.get(binding.from_node)
        if observation is None:
            raise UnsupportedQuerySpec(
                f"输入依赖 {binding.from_node} 没有可用的已接受结果"
            )
        source_spec = accepted_specs.get(binding.from_node)
        from_field = str(binding.from_field).rsplit(".", 1)[-1]
        source_rows = [
            row
            for row in (observation.result_rows or observation.preview_rows)
            if isinstance(row, dict)
        ]
        if binding.row_index is not None:
            if binding.row_index < 0 or binding.row_index >= len(source_rows):
                raise UnsupportedQuerySpec(
                    f"输入依赖 {binding.from_node} 只有 {len(source_rows)} 行，"
                    f"无法选择第 {binding.row_index + 1} 行"
                )
            source_rows = [source_rows[binding.row_index]]
        values: list[Any] = []
        for row in source_rows:
            value = row.get(from_field)
            if value is None:
                value = next(
                    (
                        item
                        for key, item in row.items()
                        if str(key).rsplit(".", 1)[-1] == from_field
                    ),
                    None,
                )
            if value is None and source_spec is not None:
                source_fields = [
                    *source_spec.dimensions,
                    *source_spec.metrics,
                    *source_spec.output_projection,
                ]
                normalized_fields = [
                    str(field).rsplit(".", 1)[-1]
                    for field in source_fields
                ]
                if from_field in normalized_fields:
                    index = normalized_fields.index(from_field)
                    if index < len(observation.columns):
                        value = row.get(observation.columns[index])
            if value in (None, "") or value in values:
                continue
            values.append(value)
        if not values:
            raise _EmptyDependencyInput(
                binding.from_node,
                binding.from_field,
            )
        if len(values) > 1 and binding.op not in {"in", "between"}:
            raise UnsupportedQuerySpec(
                f"输入依赖 {binding.from_node}.{binding.from_field} "
                f"返回了 {len(values)} 个值，但目标条件是单值 {binding.op}；"
                "请声明 row_index 选择确定的一行，或把 op 改成 in/between。"
            )
        filters = [
            item
            for item in filters
            if str(item.field).rsplit(".", 1)[-1] != binding.to_field
        ]
        bound_op = (
            "="
            if binding.op == "in" and len(values) == 1
            else binding.op
        )
        storage_value: Any = (
            values if bound_op in {"in", "between"} else values[0]
        )
        display_value = str(values[0])
        resolver_name = f"node:{binding.from_node}:{binding.from_field}"
        if _dependency_needs_dictionary(binding.to_field, spec):
            resolved = _resolve_dependency_binding(binding, values, spec)
            if str(binding.to_field).rsplit(".", 1)[-1] != "route_name":
                storage_value = resolved.storage_value
                bound_op = resolved.op
                display_value = resolved.display_value
            elif binding.op == "in":
                bound_op = "in"
                storage_value = values
            resolver_name = (
                f"{resolved.resolver}:node:{binding.from_node}:{binding.from_field}"
            )
        filters.append(
            ValueBinding(
                field=binding.to_field,
                op=bound_op,
                storage_value=storage_value,
                display_value=display_value,
                source="previous_result",
                resolver=resolver_name,
                raw_text=binding.from_node,
            )
        )
    return replace(spec, filters=tuple(filters), input_bindings=())


_RECORD_COUNT_METRICS = frozenset(
    {"count", "count_task", "task_count", "record_count"}
)


def _record_count_value(spec: Any, observation: Any) -> int | None:
    count_metrics = [
        str(metric)
        for metric in spec.metrics
        if str(metric).rsplit(".", 1)[-1] in _RECORD_COUNT_METRICS
    ]
    if len(count_metrics) != 1:
        return None
    count_field = count_metrics[0]
    count_column = result_field_map(
        spec,
        observation.columns,
    ).get(count_field)
    rows = observation.result_rows or observation.preview_rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = (
            (row.get(count_column),)
            if count_column
            else row.values()
            if len(spec.metrics) == 1
            else ()
        )
        for value in values:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
    return None


def _observation_payload(
    observation: Any,
    *,
    spec: Any,
    question: str,
    answer: str,
) -> dict[str, Any]:
    rows = observation.result_rows or observation.preview_rows
    if spec.view in {"distribution", "trend", "top_groups", "bucket_compare"}:
        logical_fields = [*spec.dimensions, *spec.metrics]
    else:
        logical_fields = [
            *spec.output_projection,
            *spec.metrics,
            *(
                order.get("field")
                for order in spec.order_by
                if isinstance(order, dict) and order.get("field")
            ),
        ]
    logical_fields = list(dict.fromkeys(str(field) for field in logical_fields))
    field_labels = result_field_map(spec, observation.columns)
    group_labels = observation_group_labels(spec.filters, field_labels)
    return {
        "question": question,
        "view": spec.view,
        "spec": {
            "subject": spec.subject,
            "view": spec.view,
            "metrics": list(spec.metrics),
            "dimensions": list(spec.dimensions),
            "output_projection": list(spec.output_projection),
            "order_by": list(spec.order_by),
        },
        "field_labels": field_labels,
        "group_labels": group_labels,
        "columns": list(observation.columns)
        or list(
            dict.fromkeys(
                [
                    *spec.output_projection,
                    *spec.dimensions,
                    *spec.metrics,
                ]
            )
        ),
        "rows": [dict(row) for row in rows],
        "reply": answer,
        "total_count": observation.total_count,
        "truncated": observation.truncated,
    }


def _trace_node_result(
    node: Any,
    compiled: Any,
    observation: Any,
    report: Any,
) -> None:
    if os.getenv("CODEX_QUERY_TRACE", "1") == "0":
        return
    record = {
        "node_id": node.id,
        "node_type": node.node_type,
        "question": node.spec.question,
        "subject": node.spec.subject,
        "view": node.spec.view,
        "metrics": list(node.spec.metrics),
        "dimensions": list(node.spec.dimensions),
        "filters": [
            {
                "field": item.field,
                "op": item.op,
                "storage_value": item.storage_value,
                "display_value": item.display_value,
                "source": item.source,
            }
            for item in node.spec.filters
        ],
        "engine": compiled.engine,
        "sql": compiled.sql,
        "columns": list(observation.columns),
        "rows_fetched": observation.rows_fetched,
        "total_count": observation.total_count,
        "truncated": observation.truncated,
        "preview_rows": list(observation.preview_rows),
        "verification": {
            "status": report.status,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "stage": issue.stage,
                }
                for issue in report.issues
            ],
        },
    }
    console_json("[query][node] ", record)


def _section_identity(node: Any) -> dict[str, Any]:
    question = str(node.spec.question or "").strip()
    title = f"{node.id}. {question[:48]}" if question else node.id
    return {
        "node_id": node.id,
        "subject": node.spec.subject,
        "title": title,
        "question": question,
        "tool": node.node_type,
        "type": "post_calc" if node.node_type == "post_calc" else "data",
        "view": node.spec.view,
        "scope_hash": scope_hash(node.spec),
        "immutable_scope_hash": immutable_scope_hash(node.spec),
        "node_type": node.node_type,
        "depends_on": list(node.depends_on),
        "spec": serialize_query_spec(node.spec),
    }


def _review_suspicions(
    plan: QueryPlan,
    sections: list[dict[str, Any]],
) -> list[ReviewSuspicion]:
    suspicions: list[ReviewSuspicion] = []
    for section in sections:
        if str(section.get("status") or "") != "repairable":
            continue
        if not (section.get("rows") or section.get("preview_rows")):
            continue
        failure_code = str(section.get("failure_code") or "")
        if failure_code in {
            "execution_failed",
            "query_compile_failed",
            "unsupported_query_spec",
        }:
            continue
        suspicions.append(
            ReviewSuspicion(
                original_question=plan.question,
                node_id=str(section.get("node_id") or ""),
                reason=str(section.get("answer") or "查询结果可能没有回答用户问题"),
                failure_layer=str(section.get("failure_stage") or "verification"),
                failure_code=failure_code,
            )
        )
    return suspicions


def _execute_plan(
    plan: QueryPlan,
    decision: UserDecision,
    candidate_set: CandidateSet | None = None,
    registry: CapabilityRegistry | None = None,
) -> AnswerPackage:
    stable_plan_id = plan_scope_hash(plan)
    blocked_nodes: dict[str, str] = {}
    blocked_node_statuses: dict[str, str] = {}
    blocked_evidence: dict[str, dict[str, Any]] = {}
    blocked_status = "ambiguous"
    try:
        plan = validate_explicit_patrol_batches(plan)
        plan = resolve_entity_comparison_batches(plan)
    except PatrolBatchResolutionError as exc:
        if exc.plan is None:
            reason = str(exc).strip() or "查询日期无法落实到真实巡检批次"
            return AnswerPackage(
                reason,
                (
                    {
                        "status": exc.status,
                        "failure_code": exc.failure_code,
                        "answer": reason,
                    },
                ),
            )
        plan = exc.plan
        blocked_nodes = dict(exc.blocked_nodes)
        blocked_node_statuses = dict(exc.blocked_node_statuses)
        blocked_evidence = dict(exc.blocked_evidence)
        blocked_status = exc.status
    candidate_by_node: dict[str, TemplateCandidate] = {}
    for candidate in (candidate_set.candidates if candidate_set else ()):
        if decision.choices.get(candidate.node_id) == candidate.candidate_id:
            candidate_by_node[candidate.node_id] = candidate
    sections: list[dict[str, Any]] = []
    accepted_observations: dict[str, Any] = {}
    accepted_specs: dict[str, Any] = {}
    unavailable_dependencies: dict[str, str] = {}
    answers_by_id: dict[str, str] = {}
    empty_scope_nodes: set[str] = set()
    inside_scope_empty_nodes: set[str] = set()
    partial_scope_empty_nodes: set[str] = set()
    partial_scope_details: dict[str, str] = {}
    count_by_scope: dict[tuple[Any, ...], int] = {}
    for node in plan.nodes:
        if node.id in blocked_nodes:
            reason = blocked_nodes[node.id]
            evidence = blocked_evidence.get(node.id, {})
            evidence_note = describe_batch_lookup_evidence(evidence)
            answer = (
                f"{reason} {evidence_note}".strip()
                if evidence_note
                else reason
            )
            unavailable_dependencies[node.id] = answer
            answers_by_id[node.id] = answer
            sections.append(
                {
                    **_section_identity(node),
                    "answer": answer,
                    "status": blocked_node_statuses.get(
                        node.id,
                        blocked_status,
                    ),
                    "failure_code": PatrolBatchResolutionError.failure_code,
                    "failure_stage": "resolution",
                    "evidence": evidence,
                }
            )
            continue
        missing_dependencies = [
            dependency
            for dependency in node.depends_on
            if dependency not in accepted_observations
        ]
        if missing_dependencies:
            if all(
                dependency in unavailable_dependencies
                for dependency in missing_dependencies
            ):
                details = "；".join(
                    str(answers_by_id[dependency]).strip()
                    for dependency in missing_dependencies
                    if str(answers_by_id.get(dependency) or "").strip()
                )
                reason = "依赖的前置结果不完整，已停止执行这一步。"
                if details:
                    reason = f"{reason}原因：{details}"
                unavailable_dependencies[node.id] = reason
                answers_by_id[node.id] = reason
                blocked_answer = (
                    "依赖的前置结果未通过，已停止执行这一步；"
                    "详细原因见对应上游步骤。"
                )
                upstream_batch_evidence = {
                    dependency: blocked_evidence[dependency]
                    for dependency in missing_dependencies
                    if dependency in blocked_evidence
                }
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": blocked_answer,
                        "status": "blocked",
                        "failure_code": "dependency_failed",
                        "failure_stage": "dependency",
                        "evidence": {
                            "dependency_ids": list(missing_dependencies),
                            "upstream_batch_resolution": upstream_batch_evidence,
                        }
                        if upstream_batch_evidence
                        else {
                            "dependency_ids": list(missing_dependencies),
                        },
                    }
                )
                continue
            reason = (
                f"依赖步骤 {', '.join(missing_dependencies)} 未通过，"
                "已停止执行这一步。"
            )
            details = "；".join(
                str(answers_by_id[dependency]).strip()
                for dependency in missing_dependencies
                if str(answers_by_id.get(dependency) or "").strip()
            )
            if details:
                reason = f"{reason}原因：{details}"
            answers_by_id[node.id] = reason
            sections.append(
                {
                    **_section_identity(node),
                    "answer": (
                        "依赖的前置结果未通过，已停止执行这一步；"
                        "详细原因见对应上游步骤。"
                    ),
                    "status": "blocked",
                    "failure_code": "dependency_failed",
                    "evidence": {
                        "dependency_ids": list(missing_dependencies),
                    },
                }
            )
            continue
        empty_dependencies = [
            dependency
            for dependency in node.depends_on
            if dependency in empty_scope_nodes
        ]
        comparison_accepts_empty_sets = (
            "dataset_comparison" in node.spec.required_assertions
            and all(
                dependency in inside_scope_empty_nodes
                or dependency in partial_scope_empty_nodes
                for dependency in empty_dependencies
            )
        )
        if (
            node.node_type == "post_calc"
            and empty_dependencies
            and not comparison_accepts_empty_sets
        ):
            reason = (
                "前置查询在现有覆盖范围内没有匹配记录，未覆盖部分又无法判断；"
                "缺少可计算的真实值，已停止这一步后置计算。"
            )
            answers_by_id[node.id] = reason
            unavailable_dependencies[node.id] = reason
            sections.append(
                {
                    **_section_identity(node),
                    "answer": reason,
                    "status": "unsupported",
                    "failure_code": "dependency_empty_scope",
                    "failure_stage": "dependency",
                    "evidence": {
                        "dependency_ids": empty_dependencies,
                    },
                }
            )
            continue
        effective_spec = node.spec
        if node.node_type == "query_data":
            if node.depends_on and not node.spec.input_bindings:
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": (
                            "依赖步骤没有声明可执行输入绑定，"
                            "无法保证使用真实的上一节点结果。"
                        ),
                        "status": "unsupported",
                        "failure_code": "missing_input_binding",
                    }
                )
                continue
            try:
                effective_spec = _materialize_inputs(
                    node.spec,
                    accepted_observations,
                    accepted_specs,
                )
            except _EmptyDependencyInput as exc:
                observation = ExecutionObservation(
                    node_id=node.id,
                    ok=True,
                    engine="dependency_short_circuit",
                    sql="",
                )
                if node.spec.view == "count":
                    observation = zero_count_observation(
                        node.spec,
                        observation,
                        registry,
                    )
                    answer = (
                        f"上游步骤 {exc.node_id} 在指定范围内返回 0 条；"
                        f"本步骤继承该空集合，计数为 0。"
                    )
                else:
                    answer = (
                        f"上游步骤 {exc.node_id} 在指定范围内返回 0 条；"
                        "本步骤没有可继续处理的记录。"
                    )
                accepted_observations[node.id] = observation
                accepted_specs[node.id] = node.spec
                answers_by_id[node.id] = answer
                empty_scope_nodes.add(node.id)
                if node.spec.view == "count":
                    count_by_scope[_scope_key(node.spec)] = 0
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": answer,
                        "status": "accepted",
                        "engine": observation.engine,
                        "shape": node.spec.view,
                        "columns": list(observation.columns),
                        "preview_rows": list(observation.preview_rows),
                        "rows": list(
                            observation.result_rows
                            or observation.preview_rows
                        ),
                        "rows_fetched": observation.rows_fetched,
                        "total_count": observation.total_count,
                        "truncated": observation.truncated,
                    }
                )
                continue
            except UnsupportedQuerySpec as exc:
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": f"无法安全执行依赖查询：{exc}",
                        "status": "unsupported",
                        "failure_code": "missing_dependency_value",
                    }
                )
                continue
        if node.node_type == "post_calc":
            upstream = {
                dependency: {
                    **_observation_payload(
                        accepted_observations[dependency],
                        spec=accepted_specs.get(dependency),
                        question=next(
                            (
                                candidate_node.spec.question
                                for candidate_node in plan.nodes
                                if candidate_node.id == dependency
                            ),
                            "",
                        ),
                        answer=answers_by_id.get(dependency, ""),
                    ),
                    **(
                        {"empty_scope": True}
                        if dependency in empty_scope_nodes
                        else {}
                    ),
                }
                for dependency in node.depends_on
            }
            payload = _default_post_calc_executor(
                {
                    "id": node.id,
                    "question": node.spec.question or plan.question,
                    "depends_on": list(node.depends_on),
                    "view": node.spec.view,
                    "metrics": list(node.spec.metrics),
                    "dimensions": list(node.spec.dimensions),
                    "required_assertions": list(node.spec.required_assertions),
                    "comparison_depends_on": list(
                        node.spec.comparison_depends_on
                    ),
                    "calculation": (
                        node.spec.calculation.to_dict()
                        if node.spec.calculation is not None
                        else None
                    ),
                },
                upstream,
            )
            if not payload.get("accepted", False):
                failure = payload.get("failure") or {}
                from ..calculations.domain.rendering import (
                    render_calculation_failure,
                )

                reason = (
                    render_calculation_failure(failure)
                    or str(
                        payload.get("reply")
                        or failure.get("reason")
                        or "后置计算没有返回可用结果"
                    )
                )
                failure_code = str(
                    failure.get("failure_code") or "post_calc_empty"
                )
                unsupported_calculation = failure_code in {
                    "calculation_zero_denominator",
                    "calculation_type_mismatch",
                }
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": (
                            f"{reason}；该指标无法可靠计算，已停止。"
                            if unsupported_calculation
                            else reason
                        ),
                        "status": (
                            "unsupported"
                            if unsupported_calculation
                            else "repairable"
                        ),
                        "failure_code": failure_code,
                        "failure": failure,
                        "calculation": (
                            (failure.get("evidence") or {}).get("calculation")
                            or {}
                        ),
                    }
                )
                continue
            answer = str(payload.get("reply") or "").strip()
            if not answer:
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": "后置计算没有返回可用结果。",
                        "status": "repairable",
                        "failure_code": "post_calc_empty",
                    }
                )
                continue
            partial_dependencies = [
                dependency
                for dependency in node.depends_on
                if dependency in partial_scope_empty_nodes
            ]
            partial_detail = "；".join(
                partial_scope_details[dependency]
                for dependency in partial_dependencies
                if partial_scope_details.get(dependency)
            )
            if partial_dependencies:
                answer = (
                    f"{answer}；覆盖边界：{partial_detail}。"
                    "新增或缺失结论只代表有记录覆盖的重叠部分；"
                    "未覆盖部分仍无法判断。"
                )
            rows = tuple(
                row for row in payload.get("rows") or () if isinstance(row, dict)
            )
            observation = ExecutionObservation(
                node_id=node.id,
                ok=True,
                engine="post_calc",
                sql="",
                columns=tuple(str(item) for item in payload.get("columns") or ()),
                row_count=len(rows),
                preview_rows=rows[:10],
                result_rows=rows,
                rows_fetched=len(rows),
                coverage={"answer": answer},
            )
            accepted_observations[node.id] = observation
            accepted_specs[node.id] = effective_spec
            answers_by_id[node.id] = answer
            sections.append(
                {
                    **_section_identity(node),
                    "answer": answer,
                    "status": "accepted",
                    "shape": "post_calc",
                    "rows_fetched": len(rows),
                    "columns": list(observation.columns),
                    "preview_rows": list(observation.preview_rows),
                    "rows": list(observation.result_rows),
                    "calculation": payload.get("calculation") or {},
                    "coverage_verdict": (
                        PARTIAL if partial_dependencies else ""
                    ),
                    "coverage_detail": partial_detail,
                }
            )
            continue
        candidate = candidate_by_node.get(node.id)
        try:
            compiled = compile_query(
                effective_spec,
                decision,
                candidate,
                _default_template_executor,
                _default_wren_executor,
                plan_id=f"{stable_plan_id}:{node.id}",
            )
        except MdlSqlPipelineError as exc:
            failure = exc.failure
            sections.append(
                {
                    **_section_identity(node),
                    "answer": mdl_failure_message(failure),
                    "status": "unsupported",
                    "failure_code": failure.code,
                    "failure_stage": failure.stage,
                    "contract_id": failure.contract_id,
                    "contract_hash": failure.contract_hash,
                    "mdl_sql": failure.mdl_sql,
                    "physical_sql": failure.physical_sql,
                    "failure_evidence": dict(failure.evidence),
                }
            )
            continue
        except UnsupportedQuerySpec as exc:
            sections.append(
                {
                    **_section_identity(node),
                    "answer": f"无法安全执行该查询：{exc}",
                    "status": "unsupported",
                    "failure_code": "unsupported_query_spec",
                }
            )
            continue
        except (ValueError, RuntimeError) as exc:
            reason = str(exc) or "查询编译失败"
            console_json(
                "[query][compile_failed] ",
                {
                    "node_id": node.id,
                    "question": effective_spec.question,
                    "candidate": candidate.candidate_id if candidate else None,
                    "reason": reason,
                },
            )
            sections.append(
                {
                    **_section_identity(node),
                    "answer": f"查询编译失败：{reason}",
                    "status": "unsupported",
                    "failure_code": "query_compile_failed",
                }
            )
            continue
        observation = execute_compiled(
            compiled,
            lambda current_kind, sql: _default_execute_sql(
                current_kind,
                sql,
                question=effective_spec.question or plan.question,
            ),
        )
        coverage_note = time_window_coverage(
            effective_spec,
            list(observation.result_rows or observation.preview_rows),
        )
        if (
            effective_spec.view in {"detail", "list"}
            and observation.total_count is None
        ):
            inherited_total = count_by_scope.get(_scope_key(effective_spec))
            if (
                inherited_total is not None
                and inherited_total >= observation.rows_fetched
            ):
                observation = replace(
                    observation,
                    total_count=inherited_total,
                    truncated=inherited_total > observation.rows_fetched,
                )
        scoped_empty = (
            bool(effective_spec.filters)
            and (
                observation.rows_fetched == 0
                or _aggregate_is_empty(effective_spec, observation)
            )
        )
        scope_facts = (
            empty_scope_facts(effective_spec)
            if scoped_empty
            else coverage_facts(effective_spec)
        )
        outside_scope_empty = (
            scoped_empty
            and is_outside_scope_empty_answerable(
                effective_spec,
                scope_facts,
            )
        )
        if (
            scoped_empty
            and scope_facts.verdict == OUTSIDE
            and not outside_scope_empty
        ):
            answer = (
                "该查询范围内没有匹配记录，并且它超出这份台账的真实数据覆盖："
                f"{scope_facts.detail}。因此这一问无法给出数值。"
            )
            answers_by_id[node.id] = answer
            unavailable_dependencies[node.id] = answer
            sections.append(
                {
                    **_section_identity(node),
                    "answer": answer,
                    "status": "unsupported",
                    "failure_code": "out_of_coverage",
                    "sql": compiled.sql,
                    "engine": compiled.engine,
                    "shape": effective_spec.view,
                    "columns": list(observation.columns),
                    "preview_rows": [],
                    "rows": [],
                    "rows_fetched": 0,
                    "coverage_detail": scope_facts.detail,
                    "coverage_verdict": OUTSIDE,
                }
            )
            continue
        if scoped_empty and scope_facts.verdict == PARTIAL:
            answer = (
                f"该查询区间只有部分位于台账覆盖内：{scope_facts.detail}。"
                "在现有记录覆盖到的重叠部分未观察到匹配记录；"
                "未覆盖部分无法判断，因此这不是整个查询区间的完整零结果。"
            )
            accepted_observations[node.id] = observation
            accepted_specs[node.id] = effective_spec
            answers_by_id[node.id] = answer
            empty_scope_nodes.add(node.id)
            partial_scope_empty_nodes.add(node.id)
            partial_scope_details[node.id] = scope_facts.detail
            sections.append(
                {
                    **_section_identity(node),
                    "answer": answer,
                    "status": "accepted",
                    "sql": compiled.sql,
                    "engine": compiled.engine,
                    "shape": effective_spec.view,
                    "columns": list(observation.columns),
                    "preview_rows": [],
                    "rows": [],
                    "rows_fetched": 0,
                    "total_count": observation.total_count,
                    "truncated": observation.truncated,
                    "coverage_detail": scope_facts.detail,
                    "coverage_verdict": PARTIAL,
                    "empty_scope": True,
                }
            )
            continue
        inside_scope_empty = (
            scoped_empty and scope_facts.verdict == INSIDE
        )
        report = verify_result(
            effective_spec,
            compiled,
            observation,
            scope_has_records=inside_scope_empty or outside_scope_empty,
        )
        display_rows, enum_filled = complete_explicit_enum_groups(
            effective_spec,
            observation,
            registry,
            scope_has_records=inside_scope_empty,
        )
        display_rows_completed = bool(enum_filled)
        recent_binding = next(
            (
                item
                for item in node.spec.input_bindings
                if str(item.to_field).rsplit(".", 1)[-1]
                == "patrol_number"
            ),
            None,
        )
        if (
            recent_binding is not None
            and "complete_recent_window"
            in effective_spec.required_assertions
            and recent_binding.from_node in accepted_observations
        ):
            display_rows, recent_filled = complete_recent_window_groups(
                effective_spec,
                replace(
                    observation,
                    columns=(
                        tuple(display_rows[0])
                        if display_rows
                        else observation.columns
                    ),
                    row_count=len(display_rows),
                    preview_rows=tuple(display_rows[:10]),
                    result_rows=tuple(display_rows),
                    rows_fetched=len(display_rows),
                ),
                accepted_specs[recent_binding.from_node],
                accepted_observations[recent_binding.from_node],
            )
            enum_filled = [*enum_filled, *recent_filled]
            display_rows_completed = True
        materialized_observation = observation
        if report.status == "accepted" and display_rows_completed:
            materialized_columns = (
                tuple(display_rows[0])
                if display_rows
                else tuple(observation.columns)
            )
            materialized_observation = replace(
                observation,
                columns=materialized_columns,
                row_count=len(display_rows),
                preview_rows=tuple(display_rows[:10]),
                result_rows=tuple(display_rows),
                rows_fetched=len(display_rows),
            )
        _trace_node_result(
            node,
            compiled,
            materialized_observation,
            report,
        )
        if report.status != "accepted":
            failure_code = (
                report.issues[0].code
                if report.issues
                else "verification_failed"
            )
            answers_by_id[node.id] = "；".join(
                issue.message for issue in report.issues
            )
            sections.append(
                {
                    **_section_identity(node),
                    "answer": "；".join(issue.message for issue in report.issues),
                    "status": report.status,
                    "failure_code": failure_code,
                    "failure_stage": (
                        report.issues[0].stage
                        if report.issues
                        else "verification"
                    ),
                    "sql": compiled.sql,
                    "engine": compiled.engine,
                    "shape": effective_spec.view,
                    "columns": list(observation.columns),
                    "preview_rows": list(observation.preview_rows),
                    "rows": list(
                        observation.result_rows or observation.preview_rows
                    ),
                    "rows_fetched": observation.rows_fetched,
                    "total_count": observation.total_count,
                    "truncated": observation.truncated,
                }
            )
            continue
        if outside_scope_empty and not enum_filled:
            if effective_spec.view == "count":
                materialized_observation = zero_count_observation(
                    effective_spec,
                    materialized_observation,
                    registry,
                )
            answer = outside_scope_empty_answer(
                effective_spec.view,
                _answer_text(
                    effective_spec,
                    materialized_observation,
                    coverage_note="",
                    registry=registry,
                ),
                scope_facts,
            )
            empty_scope_nodes.add(node.id)
        elif inside_scope_empty and not enum_filled:
            if effective_spec.view == "count" and observation.rows_fetched == 0:
                materialized_observation = zero_count_observation(
                    effective_spec,
                    materialized_observation,
                    registry,
                )
                answer = (
                    _answer_text(
                        effective_spec,
                        materialized_observation,
                        coverage_note="",
                        registry=registry,
                    )
                    + "；已核对该范围在库里有数据，"
                    "属于“范围内确实没有记录”，不是数据缺失。"
                )
            elif observation.rows_fetched == 0:
                answer = (
                    "该查询范围内没有匹配记录（0 条）；"
                    "已核对该范围在库里有数据，属于“范围内确实没有记录”，"
                    "不是数据缺失。"
                )
            else:
                # 聚合视图的空：COUNT 返回 0，或 SUM/AVG 无有效数值。
                # 不能写成“0 条明细”，也不能当成指标损坏拦下整条链路。
                answer = (
                    "该筛选范围内没有可统计的记录"
                    "（聚合结果为 0 或空，匹配记录为空或指标值全部为空）；"
                    "已核对范围覆盖，不是查询执行失败。"
                )
            empty_scope_nodes.add(node.id)
            inside_scope_empty_nodes.add(node.id)
        else:
            answer = _answer_text(
                effective_spec,
                materialized_observation,
                coverage_note=coverage_note,
                registry=registry,
            )
            if scope_facts.verdict == PARTIAL and scope_facts.detail:
                answer = f"{answer}；覆盖边界：{scope_facts.detail}"
            elif scope_facts.verdict == INSIDE and scope_facts.detail:
                answer = f"{answer}；覆盖核对：{scope_facts.detail}"
        accepted_observations[node.id] = materialized_observation
        accepted_specs[node.id] = effective_spec
        answers_by_id[node.id] = answer
        if effective_spec.view == "count":
            count = _record_count_value(
                effective_spec,
                materialized_observation,
            )
            if count is not None:
                count_by_scope[_scope_key(effective_spec)] = count
        sections.append(
            {
                **_section_identity(node),
                "answer": answer,
                "status": report.status,
                "sql": compiled.sql,
                "engine": compiled.engine,
                "shape": effective_spec.view,
                    "columns": list(materialized_observation.columns),
                "preview_rows": (
                    display_rows
                        if display_rows_completed
                        else list(materialized_observation.preview_rows)
                ),
                "rows": (
                    display_rows
                        if display_rows_completed
                    else list(
                            materialized_observation.result_rows
                            or materialized_observation.preview_rows
                    )
                ),
                    "rows_fetched": materialized_observation.rows_fetched,
                    "total_count": materialized_observation.total_count,
                    "truncated": materialized_observation.truncated,
                "coverage_note": coverage_note,
                "coverage_detail": (
                    scope_facts.detail
                ),
                "coverage_verdict": scope_facts.verdict,
                "missing_value_labels": _missing_value_labels(
                    effective_spec,
                    materialized_observation.columns,
                    registry,
                ),
                "enum_filled_groups": enum_filled,
                "missing_requested_groups": missing_explicit_top_group_labels(
                    effective_spec,
                    materialized_observation,
                    registry,
                ),
                "inherited_filters": _inherited_filter_columns(
                    effective_spec,
                    registry,
                ),
            }
        )
    for section in sections:
        if not isinstance(section, dict) or section.get("display_units"):
            continue
        section["display_units"] = _resolve_registered_units(
            section.get("spec"),
            registry,
        )
    answer = assemble_answer(sections, response_mode=plan.response_mode)
    gate = issue_review_gate(answer, _review_suspicions(plan, sections))
    return replace(answer, review_gate=gate.to_dict()) if gate else answer


def _answer_text(
    spec: Any,
    observation: Any,
    *,
    coverage_note: str | None = None,
    registry: CapabilityRegistry | None = None,
) -> str:
    def with_boundary(text: str) -> str:
        note = explicit_range_note(spec)
        if not note or note in text:
            return text
        clean = text.rstrip().removesuffix("。")
        return f"{clean}；{note}"

    registered_units = _resolve_registered_units(spec, registry)
    missing_labels = _missing_value_labels(
        spec,
        observation.columns,
        registry,
    )
    if spec.view in {"detail", "list", "top_records"}:
        if observation.rows_fetched == 0:
            return with_boundary("没有匹配记录（0 条）")
        preview = list(observation.preview_rows)
        text = "；".join(
            _format_row_text(
                row,
                subject=spec.subject,
                units=registered_units,
                missing_labels=missing_labels,
            )
            for row in preview
            if isinstance(row, dict)
        )
        if observation.total_count is not None:
            prefix = (
                f"共 {observation.total_count} 条，"
                f"当前展示前 {observation.rows_fetched} 条"
            )
            text = prefix + (f"；{text}" if text else "")
        else:
            text = (
                f"明细返回 {observation.rows_fetched} 条"
                + (f"；{text}" if text else "")
            )
        return with_boundary(text)
    result_rows = list(observation.result_rows or observation.preview_rows)
    hidden_rows = len(result_rows) > 50
    preview = result_rows[:10] if hidden_rows else result_rows
    if preview and isinstance(preview[0], dict):
        if spec.view in {"count", "sum", "avg"}:
            first = preview[0]
            if len(first) > 1:
                text = _format_row_text(
                    first,
                    subject=spec.subject,
                    units=registered_units,
                    missing_labels=missing_labels,
                ).replace("，", "；")
                note = _empty_metric_note(
                    [first],
                    missing_labels=missing_labels,
                )
                return with_boundary(f"{text}；{note}" if note else text)
            numeric_key = next(
                (
                    key
                    for key, value in first.items()
                    if str(value).replace(".", "", 1).replace("-", "", 1).isdigit()
                ),
                next(iter(first), ""),
            )
            number = first.get(numeric_key, "")
            registered_unit = (
                registered_units.get(str(numeric_key))
                or next(
                    (
                        registered_units.get(str(metric_id))
                        for metric_id in spec.metrics
                        if registered_units.get(str(metric_id))
                    ),
                    "",
                )
            )
            unit = registered_unit or _metric_unit(
                str(numeric_key),
                spec.subject,
            )
            display = _format_display_value(number)
            if display == "无有效数值":
                return with_boundary(f"{numeric_key} 无有效数值")
            if spec.view == "count":
                return with_boundary(f"共 {display}{unit}")
            return with_boundary(f"{numeric_key} {display} {unit}")
        lines = []
        for row in preview:
            lines.append(
                _format_row_text(
                    row,
                    subject=spec.subject,
                    units=registered_units,
                    missing_labels=missing_labels,
                )
            )
        text = "；".join(lines)
        if hidden_rows:
            return with_boundary(
                f"共 {observation.rows_fetched} 行，当前展示前 10 行；{text}"
            )
        coverage = (
            time_window_coverage(spec, result_rows)
            if coverage_note is None
            else coverage_note
        )
        if coverage:
            text = f"{text}；{coverage}"
        return with_boundary(text)
    coverage = (
        time_window_coverage(spec, result_rows)
        if coverage_note is None
        else coverage_note
    )
    text = f"查询返回 {observation.rows_fetched} 行"
    return with_boundary(f"{text}；{coverage}" if coverage else text)


def start_single_turn(
    question: str,
    kind: str = "mysql",
    *,
    schema: PhysicalSchemaView | None = None,
    reach: FieldReachSet | None = None,
    providers: tuple[Any, ...] | list[Any] | None = None,
    intent: str = "",
) -> TurnStart:
    schema = schema or _default_schema(kind)
    grounded = field_grounding.ground(kind, question)
    reach = reach or _field_reach_from_ground(grounded)
    registry = CapabilityRegistry.load(kind)
    subject_resolution = resolve_subject(
        question,
        "data",
        reach,
        registry,
        grounded=grounded,
        kind=kind,
    )
    try:
        for subject_attempt in range(2):
            try:
                plan = analyze_single_turn(
                    question,
                    kind,
                    schema,
                    reach,
                    audit=True,
                    registry=registry,
                    subject=subject_resolution.subject,
                    intent=intent,
                )
                break
            except QuerySubjectConflict as exc:
                if subject_attempt == 1:
                    raise
                rerouted = resolve_subject(
                    question,
                    "data",
                    reach,
                    registry,
                    grounded=grounded,
                    kind=kind,
                    conflicting_subject=exc.planner_subject,
                    previous_resolution=subject_resolution,
                )
                if not rerouted.subject:
                    raise QuerySubjectConflict(
                        initial_subject=subject_resolution.subject,
                        planner_subject=exc.planner_subject,
                        reason=(
                            "初始主体与 Planner 最终主体不一致，"
                            "主体路由复核后仍未确定唯一主体"
                        ),
                    ) from exc
                subject_resolution = rerouted
    except QuerySubjectConflict as exc:
        detail = (
            "主体路由复核一次后仍未统一："
            f"初始主体为 {exc.initial_subject or '未知'}，"
            f"Planner 最终主体为 {exc.planner_subject or '未知'}；"
            "本次查询没有执行，请补充问题最终要统计或列出的对象"
        )
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                detail,
                (
                    {
                        "status": "unsupported",
                        "failure_code": "query_subject_conflict",
                        "answer": detail,
                    },
                ),
            ),
        )
    except QueryPlanError as exc:
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                f"该查询无法执行：{exc}",
                (
                    {
                        "status": "unsupported",
                        "failure_code": "query_scope_not_found",
                        "answer": str(exc),
                    },
                ),
            ),
        )
    except LLMError as exc:
        failure_code, failure_text = describe_llm_failure(exc)
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                failure_text,
                (
                    {
                        "status": "error",
                        "failure_code": failure_code,
                        "answer": failure_text,
                    },
                ),
            ),
        )
    except QuerySpecAmbiguity as exc:
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                f"该查询需要补充明确口径：{exc}",
                (
                    {
                        "status": "ambiguous",
                        "failure_code": "query_scope_ambiguous",
                        "answer": str(exc),
                    },
                ),
            ),
        )
    except QuerySpecUnsupported as exc:
        # 能力边界是**事实**，不是"这次没问好"：直接说明不支持的原因
        # （例如只注册了 500 米 / 1 公里两种分段粒度），不要再挂
        # "请换一种说法再问，或切换模型重试"这种把责任推回用户的尾巴。
        detail = str(exc).strip().rstrip("。") or "当前数据能力不支持这一问"
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                f"{detail}。",
                (
                    {
                        "status": "unsupported",
                        "failure_code": "query_plan_not_executable",
                        "answer": f"{detail}。",
                    },
                ),
            ),
        )
    except QuerySpecError as exc:
        detail = str(exc)
        if detail.startswith("calculation_"):
            # 计算节点生不出来是**系统侧**的计划问题（重排 3 次都没给出
            # 合法的 calculation），不能把 calculation_required 这种内部代码
            # 甩给用户，也不该说成"数据里查不到"。
            detail = (
                "这一步需要做计算，但系统没能给出可执行的计算方案"
                "（缺少计算工具或操作数）；本次查询没有执行，"
                "可以重试一次或把要计算的指标和口径说得更具体"
            )
            hint = ""
        elif "未知能力缺口" in detail:
            detail = (
                "系统识别到当前数据能力不足，但没有可靠对应到已登记的"
                "能力缺口；本次查询没有执行"
            )
            hint = ""
        else:
            hint = ""
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                f"无法可靠规划该查询：{detail}。{hint}",
                (
                    {
                        "status": "unsupported",
                        "failure_code": "query_plan_not_executable",
                        "answer": f"{detail}。{hint}",
                    },
                ),
            ),
        )
    if providers is None:
        providers = (
            BuiltinTemplateProvider(),
            PresetTemplateProvider(),
            AutoTemplateProvider(),
            VerifiedExampleTemplateProvider(),
        )
    combined = CandidateSet()
    for node in plan.nodes:
        if node.node_type != "query_data":
            continue
        combined = combined.merge(resolve_templates(node.spec, schema, providers))
    if combined.candidates:
        ticket = issue_confirmation(plan, combined, schema.snapshot)
        _PENDING[ticket.token] = _PendingTurn(
            plan,
            combined,
            schema,
            registry,
            question=question,
            kind=kind,
            reach=reach,
            providers=tuple(providers),
        )
        return TurnStart(
            plan=plan,
            candidate_set=combined,
            confirmation_ticket=ticket,
        )
    answer = _execute_plan(plan, UserDecision(token=""), None, registry)
    answer = _repair_failed_post_calc(
        plan,
        answer,
        question,
        kind,
        schema,
        reach,
        registry,
        providers,
    )
    return TurnStart(plan=plan, answer=answer)


def _post_calc_failure_feedback(answer: Any) -> str:
    """把失败的计算节点整理成规划器能直接用的结构化证据。"""
    failures = []
    for section in getattr(answer, "sections", ()) or ():
        if not isinstance(section, dict):
            continue
        if str(section.get("type") or "") != "post_calc":
            continue
        if str(section.get("status") or "accepted") == "accepted":
            continue
        if (
            str(section.get("failure_code") or "")
            in {"dependency_failed", PatrolBatchResolutionError.failure_code}
            or str(section.get("failure_stage") or "")
            in {"dependency", "resolution"}
        ):
            continue
        failures.append(
            {
                "node_id": section.get("node_id"),
                "question": section.get("question"),
                "failure_code": section.get("failure_code") or "post_calc_empty",
                "reason": str(section.get("answer") or "")[:400],
                "depends_on": list(section.get("depends_on") or ()),
            }
        )
    if not failures:
        return ""
    return json.dumps(
        {
            "stage": "post_calc",
            "instruction": (
                "上一个计划的数据步骤都已经执行成功，只有计算步骤失败。"
                "请只修改失败的计算节点（换用合适的计算工具或指标），"
                "数据节点的筛选范围、时间范围、路线、病害类型必须逐字段保持不变。"
            ),
            "failures": failures,
        },
        ensure_ascii=False,
    )


def _data_scope_signature(plan: Any) -> dict[str, str]:
    return {
        str(node.id): scope_hash(node.spec)
        for node in getattr(plan, "nodes", ())
        if node.node_type == "query_data"
    }


def _repair_failed_post_calc(
    plan: Any,
    answer: Any,
    question: str,
    kind: str,
    schema: Any,
    reach: Any,
    registry: CapabilityRegistry | None,
    providers: Any,
    decision: Any = None,
    candidate_set: CandidateSet | None = None,
) -> Any:
    """计算节点失败时，把失败证据反馈给规划器重排一次。

    只修计算步骤，数据步骤必须完全不漂移；重排后仍有失败就保留原始回答，
    不会用“改到 100%”掩盖问题，也不会无限重试。

    带 `decision` 进来表示用户已经点过确认卡：重排后的数据节点直接沿用
    已确认的口径（不再弹一次卡），只允许计算节点变化。
    """
    feedback = _post_calc_failure_feedback(answer)
    if not feedback:
        return answer
    try:
        repaired = analyze_single_turn(
            question,
            kind,
            schema,
            reach,
            audit=True,
            registry=registry,
            planning_feedback=feedback,
        )
    except Exception as exc:  # noqa: BLE001 - 重排失败就保留原回答
        console_json(
            "[query][post_calc_repair] ",
            {
                "outcome": "plan_failed",
                "error": f"{type(exc).__name__}: {exc}"[:200],
            },
        )
        return answer
    if _data_scope_signature(repaired) != _data_scope_signature(plan):
        return answer
    if decision is None:
        for node in repaired.nodes:
            if node.node_type != "query_data":
                continue
            if resolve_templates(node.spec, schema, providers).candidates:
                return answer
    repaired_answer = _execute_plan(
        repaired,
        decision if decision is not None else UserDecision(token=""),
        candidate_set if decision is not None else None,
        registry,
    )
    if any(
        isinstance(section, dict)
        and str(section.get("status") or "accepted") != "accepted"
        for section in getattr(repaired_answer, "sections", ()) or ()
    ):
        return answer
    return repaired_answer


def confirm_single_turn(
    ticket_token: str,
    choices: Any,
    overrides: dict[str, dict[str, Any]],
) -> AnswerPackage | TurnStart:
    pending = _PENDING.get(str(ticket_token or ""))
    if pending is None:
        raise ValueError("confirmation ticket is missing or expired")
    decision = consume_confirmation(ticket_token, choices, overrides)
    _PENDING.pop(ticket_token, None)
    answer = _execute_plan(
        pending.plan,
        decision,
        pending.candidates,
        pending.registry,
    )
    if pending.reach is None:
        return answer
    return _repair_failed_post_calc(
        pending.plan,
        answer,
        pending.question,
        pending.kind,
        pending.schema,
        pending.reach,
        pending.registry,
        pending.providers,
        decision=decision,
        candidate_set=pending.candidates,
    )


def pending_confirmation(ticket_token: str) -> TurnStart | None:
    pending = _PENDING.get(str(ticket_token or ""))
    if pending is None:
        return None
    return TurnStart(
        plan=pending.plan,
        candidate_set=pending.candidates,
    )


def discard_pending_confirmation(ticket_token: str) -> None:
    """Drop a pending template choice without executing it."""
    _PENDING.pop(str(ticket_token or ""), None)
