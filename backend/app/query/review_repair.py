from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from ..services import field_grounding
from .analysis import analyze_single_turn
from .contracts import AnswerPackage, UserDecision, immutable_scope_hash
from .review import ReviewEvidence, serialize_query_spec
from .service import _default_schema, _execute_plan


_MAX_REPAIR_ROUNDS = 2


@dataclass(frozen=True)
class ReviewRepairResult:
    answer: AnswerPackage
    attempts: int
    stopped_reason: str = ""


def _failure_answer(
    evidence: ReviewEvidence,
    failure_code: str,
    reason: str,
) -> ReviewRepairResult:
    answer = AnswerPackage(
        reason,
        (
            {
                "node_id": evidence.suspect_node_ids[0]
                if evidence.suspect_node_ids
                else "review",
                "question": evidence.original_question,
                "status": "unsupported",
                "failure_code": failure_code,
                "answer": reason,
            },
        ),
    )
    return ReviewRepairResult(
        answer=answer,
        attempts=0,
        stopped_reason=failure_code,
    )


def _plan_signature(plan) -> str:
    payload = [
        {
            "id": node.id,
            "node_type": node.node_type,
            "depends_on": list(node.depends_on),
            "spec": serialize_query_spec(node.spec),
        }
        for node in plan.nodes
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _plan_shape_issue(
    plan,
    evidence: ReviewEvidence,
) -> str:
    original = {section.node_id: section for section in evidence.sections}
    current = {node.id: node for node in plan.nodes}
    if set(original) != set(current):
        return "plan_node_set_changed"
    suspect_ids = set(evidence.suspect_node_ids)
    for node_id, section in original.items():
        if node_id in suspect_ids:
            continue
        if serialize_query_spec(current[node_id].spec) != section.spec:
            return "non_suspect_node_changed"
    for node_id in suspect_ids:
        section = original.get(node_id)
        if section is None or not section.immutable_scope_hash:
            return "missing_original_scope"
        if immutable_scope_hash(current[node_id].spec) != section.immutable_scope_hash:
            return "scope_drift"
    return ""


def _planning_feedback(evidence: ReviewEvidence) -> str:
    return json.dumps(
        {
            "failure_code": "user_rejected_answer",
            "original_question": evidence.original_question,
            "scope_hash": evidence.scope_hash,
            "suspect_node_ids": list(evidence.suspect_node_ids),
            "allowed_changes": [
                "修可疑节点的 view、metrics、dimensions、order_by 或输出字段",
                "保持主体、过滤条件、时间、路线、病害、设备和业务编号不变",
                "保持非可疑节点完整不变",
            ],
            "nodes": [
                {
                    "node_id": item.node_id,
                    "question": item.question,
                    "view": item.view,
                    "sql": item.sql,
                    "columns": list(item.columns),
                    "result_sample": list(item.result_sample),
                    "reason": item.reason,
                    "failure_code": item.failure_code,
                    "spec": item.spec,
                }
                for item in evidence.sections
            ],
        },
        ensure_ascii=False,
        default=str,
    )


def repair_review_evidence(evidence: ReviewEvidence) -> ReviewRepairResult:
    original = {section.node_id: section for section in evidence.sections}
    first_spec = next(
        (
            original[node_id].spec
            for node_id in evidence.suspect_node_ids
            if node_id in original
        ),
        {},
    )
    kind = str(first_spec.get("kind") or "mysql")
    subject = str(first_spec.get("subject") or "")
    schema = _default_schema(kind)
    reach = field_grounding.ground(kind, evidence.original_question)
    feedback = _planning_feedback(evidence)
    previous_signature = ""
    last_answer: AnswerPackage | None = None

    for attempt in range(1, _MAX_REPAIR_ROUNDS + 1):
        try:
            plan = analyze_single_turn(
                evidence.original_question,
                kind,
                schema,
                reach,
                audit=True,
                subject=subject,
                planning_feedback=feedback,
            )
        except Exception as exc:  # noqa: BLE001
            result = _failure_answer(
                evidence,
                "repair_planner_failed",
                f"重新规划失败：{exc}",
            )
            return replace(result, attempts=attempt)
        shape_issue = _plan_shape_issue(plan, evidence)
        if shape_issue:
            result = _failure_answer(
                evidence,
                shape_issue,
                "重新规划改变了不该变化的节点或用户范围，已停止执行。",
            )
            return replace(result, attempts=attempt)
        signature = _plan_signature(plan)
        if attempt > 1 and signature == previous_signature:
            return ReviewRepairResult(
                answer=replace(
                    last_answer or AnswerPackage("两轮修复得到的计划相同，已停止重复执行。"),
                    review_gate=None,
                ),
                attempts=attempt,
                stopped_reason="repair_exhausted",
            )
        previous_signature = signature
        try:
            answer = _execute_plan(plan, UserDecision(token=""))
        except Exception as exc:  # noqa: BLE001
            result = _failure_answer(
                evidence,
                "repair_execution_failed",
                f"修复后的查询执行失败：{exc}",
            )
            return replace(result, attempts=attempt)
        last_answer = answer
        if answer.review_gate is None:
            return ReviewRepairResult(
                answer=answer,
                attempts=attempt,
                stopped_reason="",
            )
        feedback = json.dumps(
            {
                "failure_code": "repaired_answer_still_suspect",
                "original_reason": evidence.sections[0].reason
                if evidence.sections
                else "",
                "review_gate": answer.review_gate,
                "attempt": attempt,
                "max_attempts": _MAX_REPAIR_ROUNDS,
            },
            ensure_ascii=False,
            default=str,
        )

    return ReviewRepairResult(
        answer=replace(
            last_answer or AnswerPackage("两轮修复后仍未得到可靠答案。"),
            review_gate=None,
        ),
        attempts=_MAX_REPAIR_ROUNDS,
        stopped_reason="repair_exhausted",
    )
