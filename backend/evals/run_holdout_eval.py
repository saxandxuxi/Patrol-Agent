from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.query.service import TurnStart, confirm_single_turn, start_single_turn
from app.services import chat_settings, prompts
from app.services.llm import chat_json, role_model

from .capability_evidence import judge_capability_evidence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path(__file__).with_name("single_turn") / "holdout_20.yaml"
DEFAULT_DOCUMENT = PROJECT_ROOT / "docs" / "archive" / "巡查业务问题.md"


@dataclass(frozen=True)
class CaseResult:
    id: str
    ok: bool
    verdict: str
    failure_layer: str
    reason: str
    answer: str = ""


Runner = Callable[[str], dict[str, Any]]
Judge = Callable[[dict[str, Any], dict[str, Any], str], dict[str, Any]]


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def run_case(
    case: dict[str, Any],
    runner: Runner,
    judge: Judge,
    business_document: str,
) -> CaseResult:
    case_id = str(case.get("id") or "case")
    try:
        payload = runner(str(case.get("question") or ""))
    except Exception as exc:  # noqa: BLE001
        payload = {
            "answer": "",
            "error": f"{type(exc).__name__}: {exc}",
            "sections": [],
            "verification": {"status": "error", "reason": str(exc)},
        }
    try:
        verdict = judge(case, payload, business_document)
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            id=case_id,
            ok=False,
            verdict="judge_error",
            failure_layer="verification",
            reason=f"{type(exc).__name__}: {exc}",
            answer=str(payload.get("answer") or ""),
        )
    failure_verdicts = {
        "partial",
        "wrong_scope",
        "wrong_metric",
        "wrong_dimension",
        "wrong_value",
        "unverifiable",
        "execution_failure",
        "presentation_error",
    }
    section_statuses = {
        str(section.get("status") or "accepted")
        for section in payload.get("sections") or ()
        if isinstance(section, dict)
    }
    verification_status = str(
        (payload.get("verification") or {}).get("status") or ""
    )
    correct_unsupported = (
        verdict.get("accepted") is True
        and str(verdict.get("verdict") or "") == "correct_unsupported"
    )
    runtime_failure = bool(
        payload.get("error")
        or section_statuses & {"error", "failed", "repairable", "ambiguous"}
        or verification_status in {"error", "failed", "repairable", "ambiguous"}
    )
    hard_failure = runtime_failure or bool(
        not correct_unsupported
        and (
            "blocked" in section_statuses
            or verification_status == "blocked"
        )
    )
    accepted = (
        verdict.get("accepted") is True
        and str(verdict.get("verdict") or "") not in failure_verdicts
        and not hard_failure
    )
    return CaseResult(
        id=case_id,
        ok=accepted,
        verdict=str(verdict.get("verdict") or ("accepted" if accepted else "rejected")),
        failure_layer=str(verdict.get("failure_layer") or ""),
        reason=str(verdict.get("reason") or ""),
        answer=str(payload.get("answer") or ""),
    )


def _default_runner(question: str) -> dict[str, Any]:
    if not bool(chat_settings.load().get("single_turn_compiler_enabled", False)):
        raise RuntimeError("single_turn_compiler_enabled is not enabled")
    turn = start_single_turn(question)
    if turn.candidate_set and turn.confirmation_ticket:
        choices = {
            candidate.node_id: candidate.candidate_id
            for candidate in turn.candidate_set.candidates
        }
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
            "plan": _jsonable(turn.plan),
            "sections": [],
            "verification": {
                "status": "repairable",
                "reason": "查询仍需确认但没有返回答案",
            },
        }
    statuses = {
        str(section.get("status") or "accepted")
        for section in turn.answer.sections
    }
    status = "accepted"
    for candidate_status in (
        "blocked",
        "error",
        "repairable",
        "failed",
        "unsupported",
        "ambiguous",
    ):
        if candidate_status in statuses:
            status = candidate_status
            break
    return {
        "answer": turn.answer.text,
        "plan": _jsonable(turn.plan),
        "sections": _jsonable(turn.answer.sections),
        "verification": {"status": status},
    }


def _default_judge(
    case: dict[str, Any],
    payload: dict[str, Any],
    business_document: str,
) -> dict[str, Any]:
    response = chat_json(
        prompts.messages(
            "query_holdout_judge",
            business_document=business_document,
            case_json=json.dumps(case, ensure_ascii=False, default=str),
            execution_json=json.dumps(
                {
                    "answer": payload.get("answer"),
                    "error": payload.get("error"),
                    "plan": payload.get("plan"),
                    "sections": payload.get("sections") or [],
                    "verification": payload.get("verification") or {},
                },
                ensure_ascii=False,
                default=str,
            ),
            capability_evidence=json.dumps(
                judge_capability_evidence(payload),
                ensure_ascii=False,
                default=str,
            ),
        ),
        model=role_model("verify"),
    )
    if not isinstance(response, dict):
        raise ValueError("semantic judge did not return a JSON object")
    return response


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") if isinstance(data, dict) else None
    return [dict(item) for item in cases or [] if isinstance(item, dict)]


def run_suite(
    cases: list[dict[str, Any]],
    business_document: str,
    runner: Runner = _default_runner,
    judge: Judge = _default_judge,
) -> list[CaseResult]:
    return [
        run_case(case, runner, judge, business_document)
        for case in cases
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--document", default=str(DEFAULT_DOCUMENT))
    parser.add_argument("--min-pass", type=int, default=18)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases))
    if len(cases) != 20:
        print(f"expected 20 frozen cases, got {len(cases)}")
        return 2
    if args.dry_run:
        print(f"loaded {len(cases)} frozen holdout cases")
        return 0

    document = Path(args.document).read_text(encoding="utf-8")
    results = run_suite(cases, document)
    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False))
    passed = sum(result.ok for result in results)
    failed_layers: dict[str, int] = {}
    for result in results:
        if result.ok:
            continue
        layer = result.failure_layer or "unknown"
        failed_layers[layer] = failed_layers.get(layer, 0) + 1
    print(
        json.dumps(
            {
                "passed": passed,
                "total": len(results),
                "min_pass": args.min_pass,
                "failed_layers": failed_layers,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
