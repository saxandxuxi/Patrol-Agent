from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...services import static_check, wren_engine
from .models import DryPlanResult, MdlSnapshot, MdlSqlCandidate


def dry_plan_candidate(
    kind: str,
    candidate: MdlSqlCandidate,
    snapshot: MdlSnapshot,
) -> DryPlanResult:
    evidence = _candidate_evidence(candidate, snapshot)
    if candidate.failure is not None:
        return _failed(
            candidate.mdl_sql,
            "candidate_already_failed",
            evidence,
            contract_hash=candidate.contract_hash,
        )
    if not candidate.mdl_sql.strip():
        return _failed(
            candidate.mdl_sql,
            "empty_mdl_sql",
            evidence,
            contract_hash=candidate.contract_hash,
        )

    physical_sql, raw_error = wren_engine.dry_plan_mdl_sql(
        kind,
        candidate.mdl_sql,
    )
    if not physical_sql:
        hint = (
            wren_engine.compile_error_hint(
                kind,
                candidate.mdl_sql,
                raw_error,
            )
            if raw_error
            else ""
        )
        return _failed(
            candidate.mdl_sql,
            "wren_returned_no_physical_sql",
            {
                **evidence,
                "raw_error": raw_error,
                "diagnostic_hint": hint,
            },
            contract_hash=candidate.contract_hash,
        )

    physical_result = check_physical_sql(kind, physical_sql)
    return replace(
        physical_result,
        mdl_sql=candidate.mdl_sql,
        contract_hash=candidate.contract_hash,
        evidence=(
            physical_result.evidence
            if physical_result.accepted
            else {
                **evidence,
                **physical_result.evidence,
            }
        ),
    )


def check_physical_sql(
    kind: str,
    physical_sql: str,
) -> DryPlanResult:
    normalized = str(physical_sql or "").strip()
    if not normalized:
        return DryPlanResult(
            accepted=False,
            mdl_sql="",
            error="物理 SQL 为空",
            evidence={
                "failure_code": "physical_sql_invalid",
                "reason": "empty_physical_sql",
            },
        )
    catalog_kind = (
        "postgresql"
        if kind in {"postgres", "postgresql"}
        else "mysql"
    )
    try:
        problems = static_check.check_sql(
            normalized,
            catalog_kind,
        )
    except Exception as exc:
        return DryPlanResult(
            accepted=False,
            mdl_sql="",
            error="物理 SQL 静态检查异常",
            evidence={
                "failure_code": "physical_sql_invalid",
                "reason": "static_check_exception",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        )
    if problems:
        return DryPlanResult(
            accepted=False,
            mdl_sql="",
            error="物理 SQL 未通过字段目录检查",
            evidence={
                "failure_code": "physical_sql_invalid",
                "reason": "static_check_failed",
                "problems": tuple(str(item) for item in problems),
            },
        )
    return DryPlanResult(
        accepted=True,
        mdl_sql="",
        physical_sql=normalized,
    )


def _candidate_evidence(
    candidate: MdlSqlCandidate,
    snapshot: MdlSnapshot,
) -> dict[str, Any]:
    selected_models = (
        set(candidate.used_models)
        & set(snapshot.models)
    )
    if not selected_models:
        selected_models = set(snapshot.models)
    return {
        "failure_code": "dry_plan_failed",
        "contract_id": candidate.contract_id,
        "contract_hash": candidate.contract_hash,
        "allowed_models": tuple(sorted(snapshot.models)),
        "allowed_columns": {
            model: tuple(sorted(snapshot.models[model].columns))
            for model in sorted(selected_models)
        },
    }


def _failed(
    mdl_sql: str,
    reason: str,
    evidence: dict[str, Any],
    *,
    contract_hash: str = "",
) -> DryPlanResult:
    return DryPlanResult(
        accepted=False,
        mdl_sql=mdl_sql,
        physical_sql="",
        error="Wren dry-plan 未生成可执行的物理 SQL",
        evidence={
            **evidence,
            "reason": reason,
        },
        contract_hash=contract_hash,
    )
