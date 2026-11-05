from __future__ import annotations

import argparse
import json
import re
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


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _plan_specs(payload: dict[str, Any]) -> list[Any]:
    plan = payload.get("plan")
    specs = []
    for node in _value(plan, "nodes", ()) or ():
        specs.append(
            {
                "node_type": _value(node, "node_type", "query_data"),
                "depends_on": tuple(_value(node, "depends_on", ()) or ()),
                "spec": _value(node, "spec"),
            }
        )
    if specs:
        return specs
    return [
        {
            "node_type": _value(item, "node_type", "query_data"),
            "depends_on": tuple(_value(item, "depends_on", ()) or ()),
            "spec": _value(item, "spec", item),
        }
        for item in payload.get("specs") or ()
    ]


def _sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in payload.get("sections") or ()
        if isinstance(item, dict)
    ]


def _normalize(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _matches_field(required: str, candidate: Any) -> bool:
    required_key = _normalize(required)
    candidate_key = _normalize(candidate)
    if not required_key or not candidate_key:
        return False
    aliases = {
        "routelengthkm": ("routelength", "lengthkm", "路线长度", "length"),
        "routelength": ("routelength", "lengthkm", "路线长度", "length"),
        "totalarea": ("totalarea", "area", "总面积", "面积"),
        "averagearea": ("averagearea", "avgarea", "平均面积"),
        "diseasecount": ("diseasecount", "count", "数量"),
        "count": ("count", "数量"),
    }
    if required_key in candidate_key or candidate_key in required_key:
        return True
    return any(
        _normalize(alias) in candidate_key
        for alias in aliases.get(required_key, ())
    )


def _spec_values(specs: list[Any], field: str) -> list[Any]:
    values = []
    for entry in specs:
        spec = entry.get("spec")
        current = _value(spec, field, ()) or ()
        if not isinstance(current, (tuple, list)):
            current = (current,)
        values.extend(current)
    return values


def _has_dimension(specs: list[Any], required: str) -> bool:
    aliases = {
        "route": ("route", "路线", "road"),
        "damagetype": ("damage", "problemtype", "病害类型", "type"),
        "month": ("month", "月份", "time", "date"),
        "time": ("time", "date", "month", "日期", "月份"),
        "device": ("device", "equipment", "设备"),
    }
    targets = aliases.get(_normalize(required), (_normalize(required),))
    return any(
        any(_normalize(alias) in _normalize(dimension) for alias in targets)
        for dimension in _spec_values(specs, "dimensions")
    )


def _filter_text(entry: Any) -> str:
    return " ".join(
        str(_value(entry, name, "") or "")
        for name in ("field", "op", "storage_value", "display_value", "raw_text")
    )


def _has_metric(specs: list[Any], required: str) -> bool:
    candidates = list(_spec_values(specs, "metrics"))
    candidates.extend(_spec_values(specs, "output_projection"))
    return any(_matches_field(required, item) for item in candidates)


def _has_non_null_device(sections: list[dict[str, Any]]) -> bool:
    for section in sections:
        for row in section.get("preview_rows") or ():
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if "设备" not in str(key) and "device" not in str(key).lower():
                    continue
                if str(value or "").strip().upper() not in {"", "NULL", "NONE"}:
                    return True
    return False


def _has_requirement(
    requirement: str,
    specs: list[Any],
    sections: list[dict[str, Any]],
    answer: str,
) -> bool:
    key = _normalize(requirement)
    if key.startswith("groupby"):
        return _has_dimension(specs, key.removeprefix("groupby"))
    if key == "nonnulldevice":
        return _has_non_null_device(sections)
    if key in {"count", "totalarea", "routelengthkm", "routelength"}:
        if _has_metric(specs, requirement):
            return True
        if key == "totalarea":
            return "总面积" in answer or "total_area" in answer
        if key in {"routelength", "routelengthkm"}:
            return "路线长度" in answer or "route_length" in answer
        return False
    if key == "comparison":
        return any(
            word in answer
            for word in (
                "更严重",
                "最严重",
                "严重程度",
                "主要",
                "最多",
                "高于",
                "密度最高",
            )
        )
    if key == "trend":
        return any(word in answer for word in ("增加", "减少", "上升", "下降", "趋势"))
    if key == "confidence":
        return "置信" in answer
    if key == "cracktypes":
        return any("裂缝" in _filter_text(item) for item in _spec_values(specs, "filters"))
    if key == "damagetypeoil":
        return any("泛油" in _filter_text(item) for item in _spec_values(specs, "filters"))
    if key == "effectivediseaselengthgt2":
        return any(
            "length" in _normalize(_value(item, "field", ""))
            and str(_value(item, "op", "")) == ">"
            and str(_value(item, "storage_value", "")) == "2"
            for item in _spec_values(specs, "filters")
        )
    if key == "collectiontime20261102":
        return any(
            token in _filter_text(item)
            for item in _spec_values(specs, "filters")
            for token in ("2026-11-02", "20261102", "11月2", "11-02")
        )
    if key == "patrolrouterelation":
        subjects = {_normalize(item) for item in _spec_values(specs, "subject")}
        return "patrol" in subjects and _has_dimension(specs, "route")
    if key == "routeid":
        return any("route" in _normalize(_value(item, "field", "")) for item in _spec_values(specs, "filters"))
    if key == "2026q3":
        return any(
            token in _filter_text(item)
            for item in _spec_values(specs, "filters")
            for token in ("2026q3", "q3", "第三季度", "2026-07", "2026-08", "2026-09")
        )
    if key == "exactcount":
        return any(section.get("total_count") is not None for section in sections) or any(
            _normalize(_value(entry.get("spec"), "view", "")) == "count"
            for entry in specs
        )
    if key == "truncateddetail":
        return any(bool(section.get("truncated")) for section in sections)
    if key == "sumequalstotal":
        section_totals: list[float] = []
        for section in sections:
            rows = section.get("result_rows") or section.get("preview_rows") or ()
            numeric_total = 0.0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for field, value in row.items():
                    if not re.search(r"数|次数|count", str(field), re.I):
                        continue
                    try:
                        numeric_total += float(str(value))
                    except (TypeError, ValueError):
                        continue
            if numeric_total:
                section_totals.append(numeric_total)
        if section_totals:
            answer_numbers = {
                float(item)
                for item in re.findall(r"-?\d+(?:\.\d+)?", answer)
            }
            return any(total in answer_numbers for total in section_totals)
        return any(word in answer for word in ("合计", "总共", "共"))
    if key == "totalcount":
        return bool(re.search(r"共\s*\d+", answer))
    if key == "lowconfidencecount":
        return "低置信度" in answer and bool(re.search(r"\d+", answer))
    return False


def _missing_requirements(
    case: dict[str, Any],
    payload: dict[str, Any],
    answer: str,
) -> list[str]:
    specs = _plan_specs(payload)
    sections = _sections(payload)
    missing = [
        requirement
        for requirement in (str(item) for item in case.get("required_filters") or ())
        if not _has_requirement(requirement, specs, sections, answer)
    ]
    output_candidates = list(_spec_values(specs, "output_projection"))
    output_candidates.extend(_spec_values(specs, "metrics"))
    for section in sections:
        output_candidates.extend(section.get("columns") or ())
    for field in (str(item) for item in case.get("required_output_fields") or ()):
        if not any(_matches_field(field, candidate) for candidate in output_candidates):
            missing.append(field)
    return missing


def run_case(case: dict[str, Any], runner: Runner) -> CaseResult:
    case_id = str(case.get("id") or "case")
    expected = str(case.get("expected_status") or "answered")
    try:
        payload = runner(str(case.get("question") or ""))
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            id=case_id,
            ok=expected == "error",
            expected_status=expected,
            actual_status="error",
            reason=f"{type(exc).__name__}: {exc}",
        )
    answer = _answer_text(payload)
    verification = payload.get("verification") or {}
    actual = str(verification.get("status") or payload.get("status") or "")
    source = str(payload.get("source") or "")
    reason = str(verification.get("reason") or payload.get("error") or "")
    section_statuses = {
        str(section.get("status") or "accepted")
        for section in _sections(payload)
    }
    failed_section_status = next(
        (
            status
            for status in ("blocked", "failed", "error", "repairable", "unsupported")
            if status in section_statuses
        ),
        "",
    )
    if failed_section_status:
        actual = failed_section_status
        reason = reason or f"查询步骤状态为 {failed_section_status}"
    required = [str(item) for item in case.get("required_contains") or []]
    missing = [item for item in required if item not in answer]
    missing_requirements = _missing_requirements(case, payload, answer)
    if expected == "answered":
        failed_status = actual in {
            "repairable",
            "failed",
            "error",
            "blocked",
            "unsupported",
            "ambiguous",
        }
        ok = (
            bool(answer)
            and source != "error"
            and not failed_status
            and not missing
            and not missing_requirements
        )
        if not answer or source == "error":
            failure_reason = "可回答题没有产生有效答案"
        elif failed_status:
            failure_reason = f"查询状态为 {actual}"
            if reason:
                failure_reason += f"：{reason}"
        else:
            failure_reason = (
                "答案缺少必需证据："
                + "、".join([*missing, *missing_requirements])
                if missing or missing_requirements
                else ""
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
        "plan": turn.plan,
        "sections": list(turn.answer.sections),
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
