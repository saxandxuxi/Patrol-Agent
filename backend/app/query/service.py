from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..services import preset_lib, schema_catalog
from ..services.db import db_manager
from .analysis import analyze_single_turn
from .compiler import CompiledQuery, compile_query
from .confirmations import (
    ConfirmationTicket,
    consume_confirmation,
    issue_confirmation,
)
from .contracts import AnswerPackage, QueryPlan, UserDecision
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


def _default_wren_executor(spec: Any) -> str:
    raise RuntimeError("single-turn Wren compiler adapter not configured")


def _default_execute_sql(kind: str, sql: str) -> dict[str, Any]:
    return db_manager.query(kind, sql)


def assemble_answer(sections: list[dict[str, Any]]) -> AnswerPackage:
    texts: list[str] = []
    for section in sections:
        text = str(section.get("answer") or "").strip()
        if text and text not in texts:
            texts.append(text)
    if not texts:
        return AnswerPackage("查询完成，但没有可展示的数据。")
    return AnswerPackage("；".join(texts) + "。", tuple(sections))


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
    for node in plan.nodes:
        candidate = candidate_by_node.get(node.id)
        compiled = compile_query(
            node.spec,
            decision,
            candidate,
            _default_template_executor,
            _default_wren_executor,
        )
        observation = execute_compiled(compiled, _default_execute_sql)
        report = verify_result(node.spec, compiled, observation)
        if report.status != "accepted":
            sections.append(
                {
                    "node_id": node.id,
                    "answer": "；".join(issue.message for issue in report.issues),
                    "status": report.status,
                    "rows_fetched": observation.rows_fetched,
                    "total_count": observation.total_count,
                    "truncated": observation.truncated,
                }
            )
            continue
        sections.append(
            {
                "node_id": node.id,
                "answer": _answer_text(node.spec, observation),
                "status": report.status,
                "rows_fetched": observation.rows_fetched,
                "total_count": observation.total_count,
                "truncated": observation.truncated,
            }
        )
    return assemble_answer(sections)


def _answer_text(spec: Any, observation: Any) -> str:
    preview = list(observation.preview_rows)
    if spec.view == "detail":
        text = f"明细返回 {observation.rows_fetched} 条"
        if observation.total_count is not None:
            text = f"共 {observation.total_count} 条，当前展示 {observation.rows_fetched} 条"
        return text
    if preview and isinstance(preview[0], dict):
        values = "，".join(f"{key} {value}" for key, value in preview[0].items())
        return values
    return f"查询返回 {observation.rows_fetched} 行"


def start_single_turn(
    question: str,
    kind: str = "mysql",
    *,
    schema: PhysicalSchemaView | None = None,
    reach: FieldReachSet | None = None,
) -> TurnStart:
    schema = schema or _default_schema(kind)
    reach = reach or FieldReachSet()
    plan = analyze_single_turn(question, kind, schema, reach)
    providers = (
        BuiltinTemplateProvider(),
        PresetTemplateProvider(),
        AutoTemplateProvider(),
        VerifiedExampleTemplateProvider(),
    )
    combined = CandidateSet()
    for node in plan.nodes:
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
