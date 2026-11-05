from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from ..services import nl2sql, preset_lib, schema_catalog, sql_engine, wren_engine
from .analysis import analyze_single_turn
from .compiler import CompiledQuery, compile_query
from .confirmations import (
    ConfirmationTicket,
    consume_confirmation,
    issue_confirmation,
)
from .contracts import AnswerPackage, ExecutionObservation, QueryPlan, UserDecision
from .executor import execute_compiled
from .fields import FieldReachSet, PhysicalSchemaView
from .template_sources import (
    AutoTemplateProvider,
    BuiltinTemplateProvider,
    PresetTemplateProvider,
    VerifiedExampleTemplateProvider,
)
from .templates import CandidateSet, TemplateCandidate, resolve_templates
from .verifier import verify_result


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
    if candidate.contract.adapter_id == "preset":
        query = preset_lib.query_by_id(candidate.contract.native_id)
        if not query:
            return ""
        values = {
            item.field: str(item.storage_value)
            for item in spec.filters
        }
        values.update({key: str(value) for key, value in overrides.items()})
        sql, _reason = preset_lib.bind(query, values)
        return sql or ""
    raise RuntimeError(
        f"template compiler adapter not configured: {candidate.contract.adapter_id}"
    )


def _simple_spec_mdl(spec: Any) -> str | None:
    filters = list(spec.filters)
    if (
        spec.subject == "disease"
        and spec.view == "trend"
        and any(
            "time" in item or "month" in item or "date" in item
            for item in spec.dimensions
        )
    ):
        route_id = ""
        for item in filters:
            if item.field != "route_name":
                return None
            route_id = str(item.storage_value)
        time_bucket = "DATE_FORMAT(disease.collection_time, '%Y-%m')"
        from_sql = "FROM disease"
        where = ["disease.del_flag = 0"]
        if route_id:
            from_sql += " JOIN route ON disease.routes_id = route.id"
            where.append(
                "route.id = '" + route_id.replace("'", "''") + "'"
            )
        return (
            f"SELECT {time_bucket} AS 时间, COUNT(*) AS 病害数量 "
            f"{from_sql} WHERE {' AND '.join(where)} "
            f"GROUP BY {time_bucket} "
            "ORDER BY 时间"
        )
    if (
        spec.subject == "disease"
        and spec.dimensions == ("route_name",)
        and "count" in spec.metrics
        and "route_length_km" in spec.metrics
    ):
        damage_values: list[str] = []
        route_id = ""
        for item in filters:
            if item.field == "problem_type_name":
                values = (
                    item.storage_value
                    if isinstance(item.storage_value, (list, tuple))
                    else [item.storage_value]
                )
                damage_values.extend(str(value) for value in values)
            elif item.field == "route_name":
                route_id = str(item.storage_value)
            else:
                return None
        selects = [
            "disease.route_name AS 路线名称",
            "COUNT(*) AS 数量",
        ]
        joins = [
            "JOIN route ON disease.routes_id = route.id",
        ]
        where = [
            "disease.del_flag = 0",
            "disease.route_name IS NOT NULL",
        ]
        if damage_values:
            literals = ", ".join(
                "'" + value.replace("'", "''") + "'"
                for value in damage_values
            )
            joins.append(
                "JOIN damage_dict ON disease.problem_base_id = damage_dict.id"
            )
            where.append(f"damage_dict.problem_type IN ({literals})")
        if route_id:
            where.append(
                "route.id = '" + route_id.replace("'", "''") + "'"
            )
        if "total_area" in spec.metrics:
            selects.append("ROUND(SUM(disease.area), 2) AS 总面积")
        if "average_area" in spec.metrics or "avg_area" in spec.metrics:
            selects.append("ROUND(AVG(disease.area), 4) AS 平均面积")
        if "sum_disease_length" in spec.metrics:
            selects.append(
                "ROUND(SUM(disease.effective_disease_length), 4) AS 总病害长度"
            )
        selects.append("route.route_length_km AS 路线长度_km")
        limit = int(spec.limit or 500)
        return (
            f"SELECT {', '.join(selects)} "
            "FROM disease "
            f"{' '.join(joins)} "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY disease.route_name, route.route_length_km "
            "ORDER BY 数量 DESC "
            f"LIMIT {limit}"
        )
    length_filters = [
        item
        for item in filters
        if item.field in {"disease_length", "effective_disease_length"}
    ]
    if (
        spec.subject != "disease"
        or len(length_filters) != 1
        or len(filters) != 1
        or spec.view not in {"count", "sum", "list", "detail"}
    ):
        return None
    binding = length_filters[0]
    try:
        threshold = float(binding.storage_value)
    except (TypeError, ValueError):
        return None
    if binding.op not in {">", ">=", "<", "<=", "="}:
        return None
    where = (
        "disease.del_flag = 0 "
        f"AND disease.effective_disease_length {binding.op} {threshold:g}"
    )
    if spec.view == "count":
        return f"SELECT COUNT(*) AS 病害数量 FROM disease WHERE {where}"
    if spec.view == "sum":
        return (
            "SELECT ROUND(SUM(disease.effective_disease_length), 4) AS 总长度 "
            f"FROM disease WHERE {where}"
        )
    limit = int(spec.limit or 500)
    return (
        "SELECT disease.problem_number AS 病害编号, "
        "disease.effective_disease_length AS 长度, "
        "disease.pile_number AS 桩号, "
        "disease.route_name AS 路线名称, "
        "disease.collection_time AS 采集时间 "
        f"FROM disease WHERE {where} "
        f"ORDER BY disease.collection_time DESC LIMIT {limit}"
    )


def _default_wren_executor(spec: Any) -> str:
    question = str(getattr(spec, "question", "") or "").strip()
    if not question:
        raise RuntimeError("QuerySpec is missing its frozen sub-question")
    kind = str(getattr(spec, "kind", "mysql") or "mysql")
    plan = nl2sql.build_query_plan(question, kind)
    mdl_sql = (
        _simple_spec_mdl(spec)
        or nl2sql.simple_route_group_mdl(plan)
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


def assemble_answer(sections: list[dict[str, Any]]) -> AnswerPackage:
    texts: list[str] = []
    for section in sections:
        text = str(section.get("answer") or "").strip()
        if text and text not in texts:
            texts.append(text)
    if not texts:
        return AnswerPackage("查询完成，但没有可展示的数据。")
    return AnswerPackage("；".join(texts) + "。", tuple(sections))


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
    question: str,
    answer: str,
) -> dict[str, Any]:
    rows = observation.result_rows or observation.preview_rows
    return {
        "question": question,
        "columns": list(observation.columns),
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
    print(
        "[query][node] "
        + json.dumps(record, ensure_ascii=False, default=str),
        flush=True,
    )


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
    answers_by_id: dict[str, str] = {}
    count_by_scope: dict[tuple[Any, ...], int] = {}
    for node in plan.nodes:
        missing_dependencies = [
            dependency
            for dependency in node.depends_on
            if dependency not in accepted_observations
        ]
        if missing_dependencies:
            reason = (
                f"依赖步骤 {', '.join(missing_dependencies)} 未通过，"
                "已停止执行这一步。"
            )
            sections.append(
                {
                    "node_id": node.id,
                    "answer": reason,
                    "status": "blocked",
                    "failure_code": "dependency_failed",
                }
            )
            continue
        if node.node_type == "post_calc":
            upstream = {
                dependency: _observation_payload(
                    accepted_observations[dependency],
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
                        "node_id": node.id,
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
                        "node_id": node.id,
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
            answers_by_id[node.id] = answer
            sections.append(
                {
                    "node_id": node.id,
                    "answer": answer,
                    "status": "accepted",
                    "rows_fetched": len(rows),
                }
            )
            continue
        candidate = candidate_by_node.get(node.id)
        compiled = compile_query(
            node.spec,
            decision,
            candidate,
            _default_template_executor,
            _default_wren_executor,
        )
        observation = execute_compiled(
            compiled,
            lambda current_kind, sql: _default_execute_sql(
                current_kind,
                sql,
                question=node.spec.question or plan.question,
            ),
        )
        if (
            node.spec.view in {"detail", "list"}
            and observation.total_count is None
        ):
            inherited_total = count_by_scope.get(_scope_key(node.spec))
            if inherited_total is not None:
                observation = replace(
                    observation,
                    total_count=inherited_total,
                    truncated=inherited_total > observation.rows_fetched,
                )
        report = verify_result(node.spec, compiled, observation)
        _trace_node_result(node, compiled, observation, report)
        if report.status != "accepted":
            sections.append(
                {
                    "node_id": node.id,
                    "answer": "；".join(issue.message for issue in report.issues),
                    "status": report.status,
                    "columns": list(observation.columns),
                    "preview_rows": list(observation.preview_rows),
                    "rows_fetched": observation.rows_fetched,
                    "total_count": observation.total_count,
                    "truncated": observation.truncated,
                }
            )
            continue
        answer = _answer_text(node.spec, observation)
        accepted_observations[node.id] = observation
        answers_by_id[node.id] = answer
        if node.spec.view == "count":
            count = _numeric_value(observation)
            if count is not None:
                count_by_scope[_scope_key(node.spec)] = count
        sections.append(
            {
                "node_id": node.id,
                "answer": answer,
                "status": report.status,
                "columns": list(observation.columns),
                "preview_rows": list(observation.preview_rows),
                "rows_fetched": observation.rows_fetched,
                "total_count": observation.total_count,
                "truncated": observation.truncated,
            }
        )
    return assemble_answer(sections)


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
    preview = result_rows if len(result_rows) <= 50 else result_rows[:10]
    if preview and isinstance(preview[0], dict):
        if spec.view in {"count", "sum", "avg"}:
            first = preview[0]
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
        return "；".join(lines)
    return f"查询返回 {observation.rows_fetched} 行"


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
    plan = analyze_single_turn(question, kind, schema, reach)
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
