from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .models import ExamCase, ExamCaseResult, ExamManifest, ExamReport


Runner = Callable[[ExamCase], dict[str, Any]]
Judge = Callable[[ExamCase, dict[str, Any], str], dict[str, Any]]

# 运行期产物不算代码：它们由应用运行时写入（自动模板缓存等），
# 留在指纹里会让同一套题在跑完之后被判成“代码已变化”。
RUNTIME_ARTIFACT_NAMES = frozenset({"auto_templates.json"})
RUNTIME_ARTIFACT_SUFFIXES = frozenset({".log", ".cache"})
INFRASTRUCTURE_ERROR_CATEGORIES = frozenset(
    {
        "quota_exhausted",
        "rate_limited",
        "transport",
        "provider_unavailable",
    }
)


class StaleExamError(RuntimeError):
    pass


def code_fingerprint(root: Path | None = None) -> str:
    root = (root or Path(__file__).resolve().parents[3]).resolve()
    roots = (
        root / "backend" / "app",
        root / "backend" / "evals" / "promotion",
        root / "resources" / "capabilities",
        root / "prompts",
        root / "frontend" / "src",
    )
    digest = hashlib.sha256()
    for base in roots:
        if not base.exists():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if "runs" in path.parts or "__pycache__" in path.parts:
                continue
            if path.name in RUNTIME_ARTIFACT_NAMES:
                continue
            if path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            if path.suffix.lower() in RUNTIME_ARTIFACT_SUFFIXES:
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"content:{digest.hexdigest()}"


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
    for case_index, case in enumerate(manifest.cases):
        try:
            payload = runner(case)
        except Exception as exc:  # noqa: BLE001
            category = str(getattr(exc, "category", "") or "")
            if category in INFRASTRUCTURE_ERROR_CATEGORIES:
                results.append(
                    ExamCaseResult(
                        id=case.id,
                        source=case.source,
                        ok=False,
                        verdict="infrastructure_blocked",
                        failure_layer="infrastructure",
                        reason=f"{category}: {exc}",
                    )
                )
                _append_not_run(results, manifest.cases[case_index + 1 :])
                break
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
        infrastructure = payload.get("infrastructure")
        if isinstance(infrastructure, dict):
            kind = str(infrastructure.get("kind") or "provider_error")
            reason = str(infrastructure.get("reason") or kind)
            results.append(
                ExamCaseResult(
                    id=case.id,
                    source=case.source,
                    ok=False,
                    verdict="infrastructure_blocked",
                    failure_layer="infrastructure",
                    reason=f"{kind}: {reason}",
                    answer=str(payload.get("answer") or ""),
                )
            )
            _append_not_run(results, manifest.cases[case_index + 1 :])
            break
        try:
            verdict = judge(case, payload, document)
        except Exception as exc:  # noqa: BLE001
            category = str(getattr(exc, "category", "") or "")
            if category in INFRASTRUCTURE_ERROR_CATEGORIES:
                results.append(
                    ExamCaseResult(
                        id=case.id,
                        source=case.source,
                        ok=False,
                        verdict="infrastructure_blocked",
                        failure_layer="infrastructure",
                        reason=f"{category}: {exc}",
                        answer=str(payload.get("answer") or ""),
                    )
                )
                _append_not_run(results, manifest.cases[case_index + 1 :])
                break
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
        and passed_fresh + passed_old >= 18
    )
    promoted = first_attempt and thresholds_met
    infrastructure_blocked = any(
        item.failure_layer == "infrastructure" for item in results
    )
    reason = (
        "达到 15 新题 80%、5 旧题 80% 且总计 18/20 的晋级线"
        if promoted
        else "考试未完成：模型基础设施阻塞，不产生业务能力成绩"
        if infrastructure_blocked
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


def _append_not_run(
    results: list[ExamCaseResult],
    remaining: tuple[ExamCase, ...],
) -> None:
    for case in remaining:
        results.append(
            ExamCaseResult(
                id=case.id,
                source=case.source,
                ok=False,
                verdict="not_run",
                failure_layer="infrastructure",
                reason="前序模型基础设施故障，考试已停止，禁止继续消耗额度",
            )
        )


def assert_promotion(report: ExamReport) -> None:
    if not report.promoted:
        raise AssertionError(report.promotion_reason)
