from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Callable

from .models import ExamCase, ExamCaseResult, ExamManifest, ExamReport


Runner = Callable[[ExamCase], dict[str, Any]]
Judge = Callable[[ExamCase, dict[str, Any], str], dict[str, Any]]


class StaleExamError(RuntimeError):
    pass


def code_fingerprint(root: Path | None = None) -> str:
    root = (root or Path(__file__).resolve().parents[3]).resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    digest = hashlib.sha256(diff).hexdigest()
    return f"{head}:{digest}"


def _validate_manifest(manifest: ExamManifest) -> None:
    if len(manifest.fresh_cases()) != 15:
        raise ValueError("promotion exam must contain exactly 15 fresh cases")
    if len(manifest.old_cases()) != 5:
        raise ValueError("promotion exam must contain exactly 5 old cases")
    ids = [item.id for item in manifest.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("promotion exam case ids must be unique")


def run_exam(
    manifest: ExamManifest,
    *,
    runner: Runner,
    judge: Judge,
    document: str,
    fingerprint_fn: Callable[[], str] = code_fingerprint,
    first_attempt: bool = True,
) -> ExamReport:
    _validate_manifest(manifest)
    current_fingerprint = fingerprint_fn()
    if current_fingerprint != manifest.code_fingerprint:
        raise StaleExamError(
            "code fingerprint changed; generate a new 15-question exam"
        )
    results: list[ExamCaseResult] = []
    for case in manifest.cases:
        try:
            payload = runner(case)
        except Exception as exc:  # noqa: BLE001
            results.append(
                ExamCaseResult(
                    id=case.id,
                    source=case.source,
                    ok=False,
                    verdict="execution_failure",
                    failure_layer="execution",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        try:
            verdict = judge(case, payload, document)
        except Exception as exc:  # noqa: BLE001
            results.append(
                ExamCaseResult(
                    id=case.id,
                    source=case.source,
                    ok=False,
                    verdict="judge_error",
                    failure_layer="verification",
                    reason=f"{type(exc).__name__}: {exc}",
                    answer=str(payload.get("answer") or ""),
                )
            )
            continue
        accepted = (
            verdict.get("accepted") is True
            and str(verdict.get("verdict") or "")
            not in {
                "partial",
                "wrong_scope",
                "wrong_metric",
                "wrong_dimension",
                "wrong_value",
                "unverifiable",
                "execution_failure",
                "presentation_error",
            }
        )
        results.append(
            ExamCaseResult(
                id=case.id,
                source=case.source,
                ok=accepted,
                verdict=str(
                    verdict.get("verdict")
                    or ("correct_answer" if accepted else "rejected")
                ),
                failure_layer=str(verdict.get("failure_layer") or ""),
                reason=str(verdict.get("reason") or ""),
                answer=str(payload.get("answer") or ""),
            )
        )
    passed_fresh = sum(
        item.ok for item in results if item.source == "fresh"
    )
    passed_old = sum(
        item.ok for item in results if item.source == "old"
    )
    thresholds_met = (
        passed_fresh >= 12
        and passed_old >= 4
        and passed_fresh + passed_old >= 16
    )
    promoted = first_attempt and thresholds_met
    reason = (
        "达到 15 新题 80% 和 5 旧题 80% 晋级线"
        if promoted
        else "同一套题重跑只能用于诊断，不能作为晋级证据"
        if thresholds_met and not first_attempt
        else (
            f"未晋级：新题 {passed_fresh}/15，旧题 {passed_old}/5，"
            f"总计 {passed_fresh + passed_old}/20"
        )
    )
    return ExamReport(
        exam_id=manifest.exam_id,
        code_fingerprint=current_fingerprint,
        passed_fresh=passed_fresh,
        total_fresh=15,
        passed_old=passed_old,
        total_old=5,
        promoted=promoted,
        promotion_reason=reason,
        results=tuple(results),
    )


def assert_promotion(report: ExamReport) -> None:
    if not report.promoted:
        raise AssertionError(report.promotion_reason)
