from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ..services import (
    nl2sql,
    preset_lib,
    schema_catalog,
    sql_engine,
    templatizer,
    wren_engine,
)
from ..services.llm import LLMError
from ..services.logging_service import console_json
from ..services.resource_loader import load_yaml
from .analysis import analyze_single_turn
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
)
from .executor import execute_compiled
from .fields import FieldReachSet, PhysicalSchemaView
from .template_sources import (
    AutoTemplateProvider,
    BuiltinTemplateProvider,
    PresetTemplateProvider,
    VerifiedExampleTemplateProvider,
)
from .spec_mdl import UnsupportedQuerySpec, compile_spec_mdl
from .templates import CandidateSet, TemplateCandidate, resolve_templates
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


def _default_template_executor(
    candidate: TemplateCandidate,
    overrides: dict[str, Any],
    spec: Any,
) -> str:
    override_values = {key: str(value) for key, value in overrides.items()}
    if candidate.contract.adapter_id == "preset":
        query = preset_lib.query_by_id(candidate.contract.native_id)
        if not query:
            return ""
        values = {
            item.field: str(item.storage_value)
            for item in spec.filters
        }
        values.update(override_values)
        sql, _reason = preset_lib.bind(query, values)
        return sql or ""
    if candidate.contract.adapter_id == "builtin":
        registry = load_yaml("sql_templates/_index.yaml", {}) or {}
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
    from ..services import agent

    return agent._post_calc_payload(task, results)


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
    if view in {"list", "detail"}:
        if total_count is not None:
            return (
                f"{prefix}共 {int(total_count)} 条，当前展示前 {rows_fetched} 条，"
                "完整明细请在结果工作台查看。"
            )
        return f"{prefix}返回 {rows_fetched} 条，明细已生成，请在结果工作台查看。"
    if view in {"distribution", "trend", "top"}:
        return (
            f"{prefix}查询完成，共 {rows_fetched} 行，"
            "详细结果已生成，请在结果工作台查看。"
        )
    return str(section.get("answer") or "").strip()


def assemble_answer(
    sections: list[dict[str, Any]],
    response_mode: str = "data",
) -> AnswerPackage:
    if response_mode == "analysis":
        conclusions = [
            section
            for section in sections
            if str(section.get("type") or "") in {"analysis", "post_calc"}
        ]
        if conclusions:
            sections = conclusions
    texts: list[str] = []
    for section in sections:
        text = (
            _data_answer_text(section)
            if response_mode == "data"
            else str(section.get("answer") or "").strip()
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
        filters.append(
            ValueBinding(
                field=binding.to_field,
                op=bound_op,
                storage_value=values if bound_op in {"in", "between"} else values[0],
                display_value=str(values[0]),
                source="previous_result",
                resolver=f"node:{binding.from_node}:{binding.from_field}",
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


def _observation_payload(
    observation: Any,
    *,
    spec: Any,
    question: str,
    answer: str,
) -> dict[str, Any]:
    rows = observation.result_rows or observation.preview_rows
    if spec.view in {"distribution", "trend", "top", "bucket_compare"}:
        logical_fields = [*spec.dimensions, *spec.metrics]
    else:
        logical_fields = [
            *spec.output_projection,
            *(
                order.get("field")
                for order in spec.order_by
                if isinstance(order, dict) and order.get("field")
            ),
        ]
    logical_fields = list(dict.fromkeys(str(field) for field in logical_fields))
    field_labels = {
        str(field): str(label)
        for field, label in zip(logical_fields, observation.columns)
    }
    group_labels: dict[str, Any] = {}
    for item in spec.filters:
        if str(item.field).rsplit(".", 1)[-1] == "problem_type_name":
            group_labels["病害类型"] = item.display_value
        elif str(item.field).rsplit(".", 1)[-1] == "up_or_down":
            group_labels["上下行方向"] = item.display_value
    return {
        "question": question,
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
    }


def _execute_plan(
    plan: QueryPlan,
    decision: UserDecision,
    candidate_set: CandidateSet | None = None,
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
                dependency: _observation_payload(
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
                )
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
                sections.append(
                    {
                        **_section_identity(node),
                        "answer": reason,
                        "status": "repairable",
                        "failure_code": str(
                            failure.get("failure_code") or "post_calc_empty"
                        ),
                        "failure": failure,
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
        observation = execute_compiled(
            compiled,
            lambda current_kind, sql: _default_execute_sql(
                current_kind,
                sql,
                question=effective_spec.question or plan.question,
            ),
        )
        incomplete_trend = (
            effective_spec.view == "trend"
            and bool(
                _time_window_coverage(
                    effective_spec,
                    list(observation.result_rows or observation.preview_rows),
                )
            )
        )
        if incomplete_trend:
            answer = (
                _answer_text(effective_spec, observation)
                + "；时间序列不完整，无法可靠判断完整时间窗口的趋势。"
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
                }
            )
            continue
        if (
            effective_spec.view in {"detail", "list"}
            and observation.total_count is None
        ):
            inherited_total = count_by_scope.get(_scope_key(effective_spec))
            if inherited_total is not None:
                observation = replace(
                    observation,
                    total_count=inherited_total,
                    truncated=inherited_total > observation.rows_fetched,
                )
        report = verify_result(effective_spec, compiled, observation)
        _trace_node_result(node, compiled, observation, report)
        if report.status != "accepted":
            sections.append(
                {
                    **_section_identity(node),
                    "answer": "；".join(issue.message for issue in report.issues),
                    "status": report.status,
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
        answer = _answer_text(effective_spec, observation)
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
                "preview_rows": list(observation.preview_rows),
                "rows": list(observation.result_rows or observation.preview_rows),
                "rows_fetched": observation.rows_fetched,
                "total_count": observation.total_count,
                "truncated": observation.truncated,
            }
        )
    return assemble_answer(
        _terminal_sections(plan, sections),
        response_mode=plan.response_mode,
    )


def _answer_text(spec: Any, observation: Any) -> str:
    if spec.view in {"detail", "list"}:
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
        elif not text:
            text = f"明细返回 {observation.rows_fetched} 条"
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
        coverage = _time_window_coverage(spec, result_rows)
        if coverage:
            text = f"{text}；{coverage}"
        return text
    return f"查询返回 {observation.rows_fetched} 行"


def _year_month(value: Any) -> tuple[int, int] | None:
    text = str(value or "").strip().replace("年", "-").replace("月", "")
    parts = text.split("-")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _time_window_coverage(spec: Any, rows: list[dict[str, Any]]) -> str:
    month_dimension = any(
        str(field).rsplit(".", 1)[-1]
        in {"month", "collection_time_month", "create_time_month"}
        for field in spec.dimensions
    )
    if spec.view != "trend" and not (spec.view == "distribution" and month_dimension):
        return ""
    start: datetime | None = None
    end: datetime | None = None
    window_value: tuple[Any, Any] | None = None
    for item in spec.filters:
        if item.field not in {"collection_time", "create_time"}:
            continue
        try:
            if item.op == "between" and isinstance(
                item.storage_value,
                (list, tuple),
            ) and len(item.storage_value) == 2:
                start = datetime.fromisoformat(str(item.storage_value[0]))
                end = datetime.fromisoformat(str(item.storage_value[1]))
                window_value = (item.storage_value[0], item.storage_value[1])
            elif item.op in {">", ">="}:
                start = datetime.fromisoformat(str(item.storage_value))
            elif item.op in {"<", "<="}:
                end = datetime.fromisoformat(str(item.storage_value))
                if item.op == "<":
                    end -= timedelta(microseconds=1)
        except ValueError:
            continue
    if start is None or end is None or start > end:
        return ""
    expected: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        expected.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    actual = {
        f"{year:04d}-{month:02d}"
        for row in rows
        for value in row.values()
        if (parsed := _year_month(value)) is not None
        for year, month in (parsed,)
    }
    missing = [item for item in expected if item not in actual]
    if spec.view == "distribution":
        if not missing:
            return ""
        return "查询范围内以下月份没有匹配记录：" + "、".join(missing)
    if actual and missing:
        start_text = (
            window_value[0]
            if window_value is not None
            else start.strftime("%Y-%m-%d %H:%M:%S")
        )
        end_text = (
            window_value[1]
            if window_value is not None
            else end.strftime("%Y-%m-%d %H:%M:%S")
        )
        return (
            f"查询窗口 {start_text} 至 {end_text}；"
            f"结果返回月份为 {'、'.join(sorted(actual))}；"
            "未出现的月份表示查询范围内没有匹配记录。"
        )
    return ""


def start_single_turn(
    question: str,
    kind: str = "mysql",
    *,
    schema: PhysicalSchemaView | None = None,
    reach: FieldReachSet | None = None,
    providers: tuple[Any, ...] | list[Any] | None = None,
) -> TurnStart:
    schema = schema or _default_schema(kind)
    reach = reach or FieldReachSet()
    try:
        plan = analyze_single_turn(question, kind, schema, reach, audit=True)
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
        return TurnStart(
            plan=QueryPlan(question=question, nodes=()),
            answer=AnswerPackage(
                f"无法可靠规划该查询：{exc}",
                (
                    {
                        "status": "unsupported",
                        "failure_code": "query_plan_not_executable",
                        "answer": str(exc),
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
        _PENDING[ticket.token] = _PendingTurn(plan, combined, schema)
        return TurnStart(
            plan=plan,
            candidate_set=combined,
            confirmation_ticket=ticket,
        )
    return TurnStart(
        plan=plan,
        answer=_execute_plan(plan, UserDecision(token="")),
    )


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
    return _execute_plan(pending.plan, decision, pending.candidates)


def pending_confirmation(ticket_token: str) -> TurnStart | None:
    pending = _PENDING.get(str(ticket_token or ""))
    if pending is None:
        return None
    return TurnStart(
        plan=pending.plan,
        candidate_set=pending.candidates,
    )
