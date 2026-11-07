from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from ..services import (
    field_grounding,
    nl2sql,
    preset_lib,
    schema_catalog,
    sql_engine,
    templatizer,
    wren_engine,
)
from ..services.llm import LLMError
from ..services.logging_service import console_json
from ..services.resource_loader import load_capability_file
from ..services.route_resolver import QueryPlanError
from .analysis import analyze_single_turn
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
    UserDecision,
    ValueBinding,
    immutable_scope_hash,
    scope_hash,
)
from .executor import execute_compiled
from .fields import FieldReachHit, FieldReachSet, PhysicalSchemaView
from .preset_bindings import apply_overrides, values_from_spec
from .review import ReviewSuspicion, issue_review_gate, serialize_query_spec
from .result_projection import result_field_map
from .scope_facts import INSIDE, OUTSIDE, ScopeFacts, empty_scope_facts
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
        registry = load_capability_file("templates.yaml", {}) or {}
        declaration = next(
            (
                item
                for item in registry.get("templates") or []
                if isinstance(item, dict)
                and str(item.get("id") or "") == candidate.contract.native_id
            ),
            None,
        )
        if not declaration:
            return ""
        builder = str(declaration.get("builder") or "")
        if not builder:
            return ""
        plan = nl2sql.build_query_plan(spec.question, spec.kind)
        _apply_spec_to_plan(plan, spec)
        return nl2sql.build_template_sql_for(plan, builder) or ""
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


def _default_wren_executor(spec: Any) -> str:
    question = str(getattr(spec, "question", "") or "").strip()
    if not question:
        raise RuntimeError("QuerySpec is missing its frozen sub-question")
    kind = str(getattr(spec, "kind", "mysql") or "mysql")
    mdl_sql = compile_spec_mdl(spec)
    if mdl_sql:
        physical, error = wren_engine.compile_mdl_sql_verbose(kind, mdl_sql)
        if physical:
            return physical
        raise UnsupportedQuerySpec(
            f"QuerySpec 受控编译失败：{error or 'empty physical SQL'}"
        )
    plan = nl2sql.build_query_plan(question, kind)
    mdl_sql = (
        nl2sql.simple_route_group_mdl(plan)
        or nl2sql.simple_patrol_route_list_mdl(plan)
        or ""
    )
    selected_models = nl2sql.select_tables(
        question,
        kind,
        subject=str(plan.get("subject") or ""),
        route_via_patrol=bool(plan.get("route_scope_from_patrol")),
    )
    if selected_models:
        nl2sql.prune_conditions_for_selection(plan, selected_models)
    _apply_spec_to_plan(plan, spec)
    mdl_ctx = wren_engine.mdl_context(kind, focus=selected_models)
    if not mdl_sql:
        context = nl2sql.retrieve_query_context(question, kind, plan)
        wren_slots = dict(plan.get("slots") or {})
        route_match = wren_slots.get("route_match") or {}
        if route_match.get("short_name"):
            wren_slots["route"] = str(route_match["short_name"])
    else:
        context = {}
        wren_slots = dict(plan.get("slots") or {})
    feedback = ""
    last_error = ""
    required_lines = [
        *nl2sql.mdl_condition_lines(plan),
        *_spec_compiler_lines(spec),
    ]
    for _attempt in range(2):
        if not mdl_sql or last_error:
            mdl_sql = sql_engine.generate_mdl_sql(
                question,
                mdl_ctx,
                required_lines,
                wren_slots,
                feedback=feedback,
                examples=str(context.get("examples") or ""),
                errors=nl2sql.error_memory_hint(question),
                samples=nl2sql.value_samples(kind),
            )
        physical, error = wren_engine.compile_mdl_sql_verbose(kind, mdl_sql)
        if physical:
            return physical
        last_error = error or "empty physical SQL"
        feedback = f"上一版 MDL-SQL 编译失败：{last_error}"
        mdl_sql = ""
    raise RuntimeError(f"Wren compile failed: {last_error}")


def _default_execute_sql(
    kind: str,
    sql: str,
    question: str = "",
) -> dict[str, Any]:
    return sql_engine.execute_sql(kind, sql, question=question)


def _apply_spec_to_plan(plan: dict[str, Any], spec: Any) -> None:
    plan["subject"] = spec.subject
    plan["shape"] = spec.view
    slots = plan.setdefault("slots", {})
    required = set(plan.get("required_conditions") or ())
    semantic_conditions: list[dict[str, Any]] = []
    for binding in spec.filters:
        field = str(binding.field)
        if field == "problem_type_name":
            values = (
                binding.storage_value
                if isinstance(binding.storage_value, (list, tuple))
                else [binding.storage_value]
            )
            slots["damage_matches"] = [
                {"problem_type": str(value)}
                for value in values
            ]
            required.add("damage_type")
            continue
        if field == "route_name":
            slots["route_match"] = {
                "id": str(binding.storage_value),
                "short_name": binding.display_value,
            }
            required.add("route")
            continue
        if field in {"collection_time", "create_time"}:
            required.add("time_range")
            if binding.op == "between" and isinstance(binding.storage_value, (list, tuple)):
                slots["time_start"] = str(binding.storage_value[0])
                slots["time_end"] = str(binding.storage_value[1])
            elif binding.op in {">=", ">", "="}:
                slots["time_start"] = str(binding.storage_value)
            elif binding.op in {"<", "<="}:
                slots["time_end"] = str(binding.storage_value)
            continue
        if field in {"device_code", "equipment_id"}:
            required.add("device_code")
            slots["device_id"] = str(binding.storage_value)
            continue
        condition_field = (
            "effective_disease_length"
            if field == "disease_length"
            else field
        )
        semantic_conditions.append(
            {
                "field": condition_field,
                "op": binding.op,
                "value": binding.storage_value,
            }
        )
    slots["semantic_conditions"] = semantic_conditions
    plan["required_conditions"] = sorted(required)
    semantic = plan.setdefault("semantic", {})
    semantic["shape"] = spec.view
    if spec.metrics:
        semantic["metrics"] = list(spec.metrics)
    if len(spec.dimensions) == 1:
        semantic["group_by"] = spec.dimensions[0]
    if spec.output_projection:
        semantic["output_fields"] = list(spec.output_projection)


def _spec_compiler_lines(spec: Any) -> list[str]:
    lines: list[str] = []
    if spec.dimensions:
        lines.append(
            "必须输出并按以下维度分组（GROUP BY），每个组合各占一行："
            + "、".join(spec.dimensions)
        )
    if spec.metrics:
        lines.append("必须输出以下指标：" + "、".join(spec.metrics))
    if spec.output_projection:
        lines.append("必须输出以下字段：" + "、".join(spec.output_projection))
    return lines


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
        # 只有一两条记录时，用户要的是答案本身，不是“请去工作台看”。
        if 0 < len(preview) <= 3:
            detail = "；".join(
                "，".join(f"{key} {value}" for key, value in row.items())
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
        digest = _grouped_digest(result_rows)
        text = f"{prefix}查询完成，共 {shown} 行"
        if digest:
            # 结果很少时要说明“这就是全部”，否则用户分不清是完整清单
            # 还是只看了前几名。
            if shown <= 3:
                text = f"{text}（已全部列出）：{digest}"
            else:
                text = f"{text}；按排序前列：{digest}"
        notes = [
            _inherited_note(section),
            str(section.get("coverage_note") or ""),
            (
                "范围内没有" + "、".join(filled) + "的记录，已按 0 计入"
                if filled
                else ""
            ),
            _empty_metric_note(result_rows),
            "完整表格请在结果工作台查看",
        ]
        suffix = "；".join(item for item in notes if item)
        return f"{text}；{suffix}" if suffix else text
    return str(section.get("answer") or "").strip()


def _grouped_digest(
    rows: list[dict[str, Any]],
    limit: int = 3,
) -> str:
    """分组结果先给一行确定性的摘要，别只留一句“请去工作台看”。

    行数几十上百时，用户需要的是“哪几个分组最突出”。SQL 已经按指标排好序，
    这里只做展示，不重新计算，也不改变任何数值。
    """
    lines = [
        "，".join(f"{key} {value}" for key, value in row.items())
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


def _empty_metric_note(rows: list[dict[str, Any]]) -> str:
    """分组里出现空值要如实说明，不能含混成 0。"""
    for row in rows:
        if any(value in (None, "") for value in row.values()):
            return "有个别分组没有有效数值（不是 0），结果里保留为空"
    return ""


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
    if response_mode == "analysis":
        conclusions = [
            section
            for section in sections
            if str(section.get("type") or "") in {"analysis", "post_calc"}
        ]
        if conclusions:
            text_sections = conclusions
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
    要的是存储值（路线 id、枚举 code）。显示值和存储值不能互相替代，所以
    route_name 和已登记枚举字段必须回到字段字典解析；时间、桩号分段等其他
    字段保持原样传递。
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
        values: list[Any] = []
        for row in observation.result_rows or observation.preview_rows:
            if not isinstance(row, dict):
                continue
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
            raise UnsupportedQuerySpec(
                f"输入依赖 {binding.from_node}.{binding.from_field} 没有可传递的值"
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
            storage_value = resolved.storage_value
            bound_op = resolved.op
            display_value = resolved.display_value
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


def _numeric_value(observation: Any) -> int | None:
    rows = observation.result_rows or observation.preview_rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
    return None


# 这些枚举取值的意思是“没区分/全部”，不是真实的一类，补 0 行只会添噪音。
_UNSPECIFIED_ENUM_LABELS = frozenset(
    {"全部", "全部/未区分", "全部/未知", "未区分", "未知", "未填写", "未识别"}
)


def _completed_enum_rows(
    spec: Any,
    observation: Any,
    registry: CapabilityRegistry | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按枚举维度分组、且只统计数量时，把枚举里没出现的取值补成 0 行。

    结果行回答“这次命中了哪些取值”，枚举回答“这个字段有哪些合法取值”，
    两者是不同的视图。用户问“上行、下行各多少处”时，缺的一侧必须是
    显式的 0，而不是让人自己去推断是不是漏查了。只补数量，不补平均值。
    """
    rows = list(observation.result_rows or observation.preview_rows)
    metrics = tuple(str(item).lower() for item in spec.metrics)
    if (
        registry is None
        or not rows
        or not isinstance(rows[0], dict)
        or spec.view not in {"distribution", "top_groups"}
        or len(spec.dimensions) != 1
        or not metrics
        # 只有可加指标缺值才等于 0；平均值、最大最小值、占比缺值不是 0。
        or any(
            not (item == "count" or item.startswith(("total_", "sum_")))
            for item in metrics
        )
    ):
        return rows, []
    dimension_id = str(spec.dimensions[0]).rsplit(".", 1)[-1]
    field = registry.snapshot.fields.get(spec.subject, {}).get(dimension_id)
    if field is None or not field.physical_table or not field.physical_column:
        return rows, []
    enum = registry.snapshot.enums.get(
        (field.physical_table, field.physical_column)
    )
    if enum is None:
        return rows, []
    column = result_field_map(spec, tuple(rows[0])).get(dimension_id)
    if not column:
        return rows, []
    wanted: list[str] | None = None
    for item in spec.filters:
        if str(item.field).rsplit(".", 1)[-1] != dimension_id:
            continue
        value = item.storage_value
        wanted = [
            str(entry)
            for entry in (
                value if isinstance(value, (list, tuple)) else [value]
            )
        ]
    present = {str(row.get(column) or "").strip() for row in rows}
    filled: list[str] = []
    for value in enum.values.values():
        label = str(value.display or value.label or "").strip()
        if wanted is not None and not (
            {value.code, value.label, value.display} & set(wanted)
        ):
            continue
        if (
            not label
            or label in present
            or label in _UNSPECIFIED_ENUM_LABELS
            or str(value.label or "").strip() in _UNSPECIFIED_ENUM_LABELS
        ):
            continue
        filled.append(label)
        rows.append(
            {
                **{key: 0 for key in rows[0]},
                column: label,
            }
        )
    return rows, filled


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
    group_labels: dict[str, Any] = {}
    for item in spec.filters:
        filter_field = str(item.field).rsplit(".", 1)[-1]
        if filter_field == "problem_type_name":
            group_labels["病害类型"] = item.display_value
        elif filter_field == "up_or_down":
            group_labels["上下行方向"] = item.display_value
        elif str(getattr(item, "source", "")) == "previous_result":
            # 上一节点传下来的条件也是这次结果的一部分，不能丢。
            label = field_labels.get(filter_field) or filter_field
            group_labels[str(label)] = item.display_value
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
    count_by_scope: dict[tuple[Any, ...], int] = {}
    for node in plan.nodes:
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
            sections.append(
                {
                    **_section_identity(node),
                    "answer": reason,
                    "status": "blocked",
                    "failure_code": "dependency_failed",
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
                reason = str(
                    payload.get("reply")
                    or failure.get("reason")
                    or "后置计算没有返回可用结果"
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
            )
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
        incomplete_trend = (
            effective_spec.view == "trend" and bool(coverage_note)
        )
        if incomplete_trend:
            answer = (
                _answer_text(
                    effective_spec,
                    observation,
                    coverage_note="",
                )
                + "；"
                + (
                    coverage_note
                    or "时间序列不完整，无法可靠判断完整时间窗口的趋势。"
                )
            )
            unavailable_dependencies[node.id] = answer
            sections.append(
                {
                    **_section_identity(node),
                    "answer": answer,
                    "status": "unsupported",
                    "failure_code": "incomplete_time_coverage",
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
                    "coverage_note": coverage_note,
                }
            )
            continue
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
            observation.rows_fetched == 0
            and bool(effective_spec.filters)
        )
        scope_facts = (
            empty_scope_facts(effective_spec)
            if scoped_empty
            else ScopeFacts()
        )
        if scoped_empty and scope_facts.verdict == OUTSIDE:
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
                }
            )
            continue
        scoped_empty = scoped_empty and scope_facts.verdict == INSIDE
        report = verify_result(
            effective_spec,
            compiled,
            observation,
            scope_has_records=scoped_empty,
        )
        display_rows, enum_filled = _completed_enum_rows(
            effective_spec,
            observation,
            registry,
        )
        _trace_node_result(node, compiled, observation, report)
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
        if scoped_empty:
            answer = (
                "该查询范围内没有匹配记录（0 条）；"
                "已核对该范围在库里有数据，属于“范围内确实没有记录”，"
                "不是数据缺失。"
            )
            empty_scope_nodes.add(node.id)
        else:
            answer = _answer_text(
                effective_spec,
                (
                    replace(
                        observation,
                        preview_rows=tuple(display_rows),
                        result_rows=tuple(display_rows),
                    )
                    if enum_filled
                    else observation
                ),
                coverage_note=coverage_note,
            )
        accepted_observations[node.id] = observation
        accepted_specs[node.id] = effective_spec
        answers_by_id[node.id] = answer
        if effective_spec.view == "count":
            count = _numeric_value(observation)
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
                "columns": list(observation.columns),
                "preview_rows": (
                    display_rows
                    if enum_filled
                    else list(observation.preview_rows)
                ),
                "rows": (
                    display_rows
                    if enum_filled
                    else list(
                        observation.result_rows or observation.preview_rows
                    )
                ),
                "rows_fetched": observation.rows_fetched,
                "total_count": observation.total_count,
                "truncated": observation.truncated,
                "coverage_note": coverage_note,
                "enum_filled_groups": enum_filled,
                "inherited_filters": _inherited_filter_columns(
                    effective_spec,
                    registry,
                ),
            }
        )
    answer = assemble_answer(sections, response_mode=plan.response_mode)
    gate = issue_review_gate(answer, _review_suspicions(plan, sections))
    return replace(answer, review_gate=gate.to_dict()) if gate else answer


def _answer_text(
    spec: Any,
    observation: Any,
    *,
    coverage_note: str | None = None,
) -> str:
    if spec.view in {"detail", "list", "top_records"}:
        preview = list(observation.preview_rows)
        text = "；".join(
            "，".join(f"{key} {value}" for key, value in row.items())
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
        return text
    result_rows = list(observation.result_rows or observation.preview_rows)
    hidden_rows = len(result_rows) > 50
    preview = result_rows[:10] if hidden_rows else result_rows
    if preview and isinstance(preview[0], dict):
        if spec.view in {"count", "sum", "avg"}:
            first = preview[0]
            if len(first) > 1:
                values = {
                    "disease": "处",
                    "patrol": "次",
                    "route": "条",
                    "section": "段",
                    "work_order": "条",
                }
                parts = []
                for key, value in first.items():
                    key_text = str(key)
                    if "面积" in key_text or "area" in key_text.lower():
                        unit = "㎡"
                    elif "长度" in key_text or "length" in key_text.lower():
                        unit = "米"
                    else:
                        unit = values.get(spec.subject, "条")
                    parts.append(f"{key_text} {value}{unit}")
                return "；".join(parts)
            numeric_key = next(
                (
                    key
                    for key, value in first.items()
                    if str(value).replace(".", "", 1).replace("-", "", 1).isdigit()
                ),
                next(iter(first), ""),
            )
            number = first.get(numeric_key, "")
            units = {
                "disease": "处",
                "patrol": "次",
                "route": "条",
                "section": "段",
                "work_order": "条",
                "bridge": "座",
                "tunnel": "座",
                "slope": "处",
            }
            metric_text = " ".join(
                [
                    *[str(item) for item in spec.metrics],
                    *[str(item) for item in spec.output_projection],
                    *[str(item) for item in observation.columns],
                ]
            ).lower()
            if "length" in metric_text or "长度" in metric_text:
                unit = "米"
            elif "area" in metric_text or "面积" in metric_text:
                unit = "㎡"
            else:
                unit = units.get(spec.subject, "条")
            if spec.view == "count":
                return f"共 {number}{unit}"
            return f"{numeric_key} {number} {unit}"
        lines = []
        for row in preview:
            lines.append("，".join(f"{key} {value}" for key, value in row.items()))
        text = "；".join(lines)
        if hidden_rows:
            return (
                f"共 {observation.rows_fetched} 行，当前展示前 10 行；{text}"
            )
        coverage = (
            time_window_coverage(spec, result_rows)
            if coverage_note is None
            else coverage_note
        )
        if coverage:
            text = f"{text}；{coverage}"
        return text
    coverage = (
        time_window_coverage(spec, result_rows)
        if coverage_note is None
        else coverage_note
    )
    text = f"查询返回 {observation.rows_fetched} 行"
    return f"{text}；{coverage}" if coverage else text


def start_single_turn(
    question: str,
    kind: str = "mysql",
    *,
    schema: PhysicalSchemaView | None = None,
    reach: FieldReachSet | None = None,
    providers: tuple[Any, ...] | list[Any] | None = None,
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
    )
    try:
        plan = analyze_single_turn(
            question,
            kind,
            schema,
            reach,
            audit=True,
            registry=registry,
            subject=subject_resolution.subject,
        )
    except nl2sql.QueryPlanError as exc:
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
    except LLMError:
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                "模型没有返回可解析的结构化查询计划，请重试或切换模型。",
                (
                    {
                        "status": "error",
                        "failure_code": "llm_output_invalid",
                        "answer": "模型输出格式无效，当前查询未执行。",
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
    except QuerySpecError as exc:
        detail = str(exc)
        hint = (
            "这是查询计划内部的视图名不合法，不代表数据里查不到；"
            "请换一种说法再问，或切换模型重试。"
            if "不支持的查询视图" in detail
            else "请换一种说法再问，或切换模型重试。"
        )
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
        _PENDING[ticket.token] = _PendingTurn(plan, combined, schema, registry)
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
) -> Any:
    """计算节点失败时，把失败证据反馈给规划器重排一次。

    只修计算步骤，数据步骤必须完全不漂移；重排后仍有失败就保留原始回答，
    不会用“改到 100%”掩盖问题，也不会无限重试。
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
    for node in repaired.nodes:
        if node.node_type != "query_data":
            continue
        if resolve_templates(node.spec, schema, providers).candidates:
            return answer
    repaired_answer = _execute_plan(
        repaired,
        UserDecision(token=""),
        None,
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
    return _execute_plan(
        pending.plan,
        decision,
        pending.candidates,
        pending.registry,
    )


def pending_confirmation(ticket_token: str) -> TurnStart | None:
    pending = _PENDING.get(str(ticket_token or ""))
    if pending is None:
        return None
    return TurnStart(
        plan=pending.plan,
        candidate_set=pending.candidates,
    )
