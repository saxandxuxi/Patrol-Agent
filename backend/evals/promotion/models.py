from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExamCase:
    id: str
    category: str
    question: str
    focus: str = ""
    source: str = "fresh"
    coverage_slot: str = ""


@dataclass(frozen=True)
class ExamManifest:
    exam_id: str
    generated_at: str
    document_hash: str
    seed: int
    code_fingerprint: str
    fresh_case_ids: tuple[str, ...]
    old_case_ids: tuple[str, ...]
    cases: tuple[ExamCase, ...]
    answers: dict[str, bool] = field(default_factory=dict)

    def fresh_cases(self) -> tuple[ExamCase, ...]:
        fresh_ids = set(self.fresh_case_ids)
        return tuple(item for item in self.cases if item.id in fresh_ids)

    def old_cases(self) -> tuple[ExamCase, ...]:
        old_ids = set(self.old_case_ids)
        return tuple(item for item in self.cases if item.id in old_ids)


@dataclass(frozen=True)
class ExamCaseResult:
    id: str
    source: str
    ok: bool
    verdict: str
    failure_layer: str
    reason: str
    answer: str = ""


@dataclass(frozen=True)
class ExamReport:
    exam_id: str
    code_fingerprint: str
    passed_fresh: int
    total_fresh: int
    passed_old: int
    total_old: int
    promoted: bool
    promotion_reason: str
    results: tuple[ExamCaseResult, ...]

    @property
    def passed_total(self) -> int:
        return self.passed_fresh + self.passed_old

    @property
    def total(self) -> int:
        return self.total_fresh + self.total_old

    def to_dict(self) -> dict[str, Any]:
        return {
            "exam_id": self.exam_id,
            "code_fingerprint": self.code_fingerprint,
            "passed_fresh": self.passed_fresh,
            "total_fresh": self.total_fresh,
            "passed_old": self.passed_old,
            "total_old": self.total_old,
            "passed_total": self.passed_total,
            "total": self.total,
            "promoted": self.promoted,
            "promotion_reason": self.promotion_reason,
            "results": [
                {
                    "id": item.id,
                    "source": item.source,
                    "ok": item.ok,
                    "verdict": item.verdict,
                    "failure_layer": item.failure_layer,
                    "reason": item.reason,
                    "answer": item.answer,
                }
                for item in self.results
            ],
        }
