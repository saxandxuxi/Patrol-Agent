from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

from .contracts import AnswerPackage


_TOKEN_TTL_SECONDS = 600.0
_PENDING: dict[str, tuple[float, ReviewEvidence]] = {}
_CONSUMED: dict[str, float] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReviewSuspicion:
    original_question: str
    node_id: str
    reason: str
    failure_layer: str = "verification"
    failure_code: str = ""


@dataclass(frozen=True)
class ReviewNodeEvidence:
    node_id: str
    question: str
    view: str
    sql: str
    columns: tuple[str, ...]
    result_sample: tuple[dict[str, Any], ...]
    reason: str
    failure_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "question": self.question,
            "view": self.view,
            "sql": self.sql,
            "columns": list(self.columns),
            "result_sample": [dict(row) for row in self.result_sample],
            "reason": self.reason,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True)
class ReviewGate:
    review_token: str
    original_question: str
    title: str
    reason: str
    nodes: tuple[ReviewNodeEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_token": self.review_token,
            "original_question": self.original_question,
            "title": self.title,
            "reason": self.reason,
            "nodes": [item.to_dict() for item in self.nodes],
        }


@dataclass(frozen=True)
class ReviewEvidence:
    original_question: str
    scope_hash: str
    suspect_node_ids: tuple[str, ...]
    sections: tuple[ReviewNodeEvidence, ...]
    decision: Literal["accept", "reject"] = "reject"


def _row_sample(section: dict[str, Any], limit: int = 5) -> tuple[dict[str, Any], ...]:
    rows = section.get("rows") or section.get("preview_rows") or ()
    return tuple(
        dict(row)
        for row in list(rows)[:limit]
        if isinstance(row, dict)
    )


def issue_review_gate(
    answer: AnswerPackage,
    suspicions: list[ReviewSuspicion] | tuple[ReviewSuspicion, ...],
) -> ReviewGate | None:
    if not suspicions:
        return None
    section_by_node = {
        str(section.get("node_id") or ""): section
        for section in answer.sections
        if isinstance(section, dict)
    }
    nodes: list[ReviewNodeEvidence] = []
    evidence_sections: list[ReviewNodeEvidence] = []
    for suspicion in suspicions:
        section = section_by_node.get(suspicion.node_id) or {}
        evidence = ReviewNodeEvidence(
            node_id=suspicion.node_id,
            question=str(
                section.get("question")
                or suspicion.original_question
            ),
            view=str(section.get("view") or ""),
            sql=str(section.get("sql") or ""),
            columns=tuple(str(item) for item in section.get("columns") or ()),
            result_sample=_row_sample(section),
            reason=suspicion.reason,
            failure_code=suspicion.failure_code,
        )
        nodes.append(evidence)
        evidence_sections.append(evidence)
    original_question = str(
        suspicions[0].original_question
        or nodes[0].question
    )
    reasons = list(
        dict.fromkeys(
            suspicion.reason.strip()
            for suspicion in suspicions
            if suspicion.reason.strip()
        )
    )
    scope_hashes = tuple(
        dict.fromkeys(
            str(
                (section_by_node.get(suspicion.node_id) or {}).get(
                    "scope_hash"
                )
                or ""
            )
            for suspicion in suspicions
        )
    )
    token = uuid.uuid4().hex
    gate = ReviewGate(
        review_token=token,
        original_question=original_question,
        title="我不确定这次结果是否回答了你",
        reason="；".join(reasons) or "查询结果与问题契约存在疑点",
        nodes=tuple(nodes),
    )
    evidence = ReviewEvidence(
        original_question=original_question,
        scope_hash="|".join(item for item in scope_hashes if item),
        suspect_node_ids=tuple(suspicion.node_id for suspicion in suspicions),
        sections=tuple(evidence_sections),
    )
    now = time.monotonic()
    with _LOCK:
        for key, (created_at, _item) in list(_PENDING.items()):
            if now - created_at > _TOKEN_TTL_SECONDS:
                _PENDING.pop(key, None)
        _PENDING[token] = (now, evidence)
    return gate


def consume_review_gate(
    token: str,
    decision: Literal["accept", "reject"],
) -> ReviewEvidence | None:
    if decision not in {"accept", "reject"}:
        raise ValueError(f"unsupported review decision: {decision}")
    with _LOCK:
        item = _PENDING.pop(str(token or ""), None)
    if item is None:
        return None
    created_at, evidence = item
    if time.monotonic() - created_at > _TOKEN_TTL_SECONDS:
        return None
    with _LOCK:
        _CONSUMED[str(token)] = time.monotonic()
    return replace(evidence, decision=decision)


def review_gate_error(token: str) -> str:
    with _LOCK:
        now = time.monotonic()
        for key, created_at in list(_CONSUMED.items()):
            if now - created_at > _TOKEN_TTL_SECONDS:
                _CONSUMED.pop(key, None)
        if str(token or "") in _CONSUMED:
            return "review_token_consumed"
    return "review_token_missing"
