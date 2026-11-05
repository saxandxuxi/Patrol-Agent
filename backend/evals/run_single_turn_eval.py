from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.query.contracts import AnswerPackage
from app.query.service import (
    TurnStart,
    confirm_single_turn,
    start_single_turn,
)


@dataclass(frozen=True)
class CaseResult:
    id: str
    ok: bool
    expected_status: str
    actual_status: str
    reason: str
    answer: str = ""


Runner = Callable[[str], dict[str, Any]]


def _answer_text(payload: dict[str, Any]) -> str:
    return str(payload.get("answer") or payload.get("reply") or "").strip()


def run_case(case: dict[str, Any], runner: Runner) -> CaseResult:
    case_id = str(case.get("id") or "case")
    expected = str(case.get("expected_status") or "answered")
    payload = runner(str(case.get("question") or ""))
    answer = _answer_text(payload)
    verification = payload.get("verification") or {}
    actual = str(verification.get("status") or payload.get("status") or "")
    source = str(payload.get("source") or "")
    reason = str(verification.get("reason") or payload.get("error") or "")
    required = [str(item) for item in case.get("required_contains") or []]
    missing = [item for item in required if item not in answer]
    if expected == "answered":
        failed_status = actual in {
            "repairable",
            "failed",
            "error",
            "unsupported",
            "ambiguous",
        }
        ok = bool(answer) and source != "error" and not failed_status and not missing
        failure_reason = (
            "可回答题没有产生有效答案"
            if not answer or source == "error" or failed_status
            else ("答案缺少必需内容：" + "、".join(missing) if missing else "")
        )
    elif expected == "unsupported":
        ok = bool(answer and actual == "unsupported" and reason)
        failure_reason = "" if ok else "超能力问题没有给出具体原因"
    elif expected == "partial":
        ok = bool(answer and actual == "partial")
        failure_reason = "" if ok else "部分完成状态没有被明确返回"
    else:
        ok = actual == expected
        failure_reason = "" if ok else f"期望状态 {expected}，实际 {actual}"
    return CaseResult(
        id=case_id,
        ok=ok,
        expected_status=expected,
        actual_status=actual or ("answered" if answer else "failed"),
        reason=failure_reason,
        answer=answer,
    )


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") if isinstance(data, dict) else None
    return [dict(item) for item in cases or [] if isinstance(item, dict)]


def _default_runner(question: str) -> dict[str, Any]:
    turn = start_single_turn(question)
    if turn.candidate_set and turn.confirmation_ticket:
        choices: dict[str, str] = {}
        for candidate in turn.candidate_set.candidates:
            choices.setdefault(candidate.node_id, candidate.candidate_id)
        answer = confirm_single_turn(
            turn.confirmation_ticket.token,
            choices,
            {},
        )
        if isinstance(answer, TurnStart):
            turn = answer
        else:
            turn = TurnStart(plan=turn.plan, answer=answer)
    if turn.answer is None:
        return {
            "answer": "",
            "verification": {
                "status": "repairable",
                "reason": "查询仍需确认但没有返回答案",
            },
        }
    statuses = {
        str(section.get("status") or "accepted")
        for section in turn.answer.sections
    }
    status = "repairable" if "repairable" in statuses else "accepted"
    return {
        "answer": turn.answer.text,
        "verification": {
            "status": status,
            "reason": "" if status == "accepted" else "查询步骤未通过验收",
        },
    }


def run_suite(
    cases: list[dict[str, Any]],
    runner: Runner = _default_runner,
) -> list[CaseResult]:
    return [run_case(case, runner) for case in cases]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        default=str(Path(__file__).with_name("single_turn") / "cases.yaml"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cases = load_cases(Path(args.cases))
    if args.dry_run:
        print(f"loaded {len(cases)} single-turn cases")
        return 0
    results = run_suite(cases)
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
