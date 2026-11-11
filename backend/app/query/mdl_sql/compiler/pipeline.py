from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ...capability_registry import CapabilityRegistry
from ..audit import audit_mdl_sql
from ..catalog import load_mdl_snapshot
from ..context.prompt_context import (
    build_prompt_context,
    serialize_prompt_context,
)
from ..context.relationships import build_relationship_subgraph
from ..contract_builder import build_mdl_query_contract
from ..dry_plan import dry_plan_candidate
from ..errors import (
    MdlSqlContextError,
    MdlSqlContractError,
    MdlSqlFallbackError,
    MdlSqlPipelineError,
    MdlSqlRelationshipError,
)
from ..models import (
    CompilerFailure,
    DryPlanResult,
    MdlQueryContract,
    MdlSnapshot,
    MdlSqlCandidate,
    MdlSqlPipelineResult,
    PromptContextBundle,
    RelationshipSubgraph,
    SqlAuditReport,
)
from .deterministic import compile_contract_to_mdl_sql
from .llm_fallback import generate_mdl_sql_once

from ....query.contracts import QuerySpec


MdlSqlGenerator = Callable[
    [PromptContextBundle, CompilerFailure],
    MdlSqlCandidate,
]


def run_mdl_sql_pipeline(
    spec: QuerySpec,
    *,
    plan_id: str = "",
    kind: str | None = None,
    registry: CapabilityRegistry | None = None,
    mdl_snapshot: MdlSnapshot | None = None,
    generate_mdl_sql: MdlSqlGenerator | None = None,
) -> MdlSqlPipelineResult:
    registry = registry or CapabilityRegistry.load()
    resolved_kind = str(kind or spec.kind or "mysql")
    snapshot = mdl_snapshot or load_mdl_snapshot(resolved_kind)
    result = preflight_mdl_sql(
        spec,
        plan_id=plan_id,
        kind=resolved_kind,
        registry=registry,
        mdl_snapshot=snapshot,
    )
    if result is not None:
        return result

    contract = _build_contract(spec, registry, snapshot, plan_id)
    context = _build_context(
        contract,
        registry,
        snapshot,
        kind=resolved_kind,
    )
    subgraph = _build_subgraph(contract, snapshot, registry)
    policy = registry.snapshot.mdl_compilation
    deterministic = compile_contract_to_mdl_sql(contract, context)
    deterministic_failure = deterministic.failure
    if (
        deterministic_failure is None
        and deterministic.mdl_sql.strip()
    ):
        raise MdlSqlPipelineError(
            _failure(
                contract,
                code="compiler_exception",
                stage="deterministic",
                reason="preflight_not_covered_but_sql_was_generated",
            )
        )
    if (
        deterministic_failure is not None
        and deterministic_failure.code != "compiler_not_covered"
    ):
        raise MdlSqlPipelineError(deterministic_failure)
    return _run_fallback(
        contract=contract,
        context=context,
        subgraph=subgraph,
        snapshot=snapshot,
        kind=resolved_kind,
        policy=policy,
        deterministic_failure=(
            deterministic_failure
            or _failure(
                contract,
                code="compiler_not_covered",
                stage="deterministic",
                reason="empty_deterministic_mdl_sql",
            )
        ),
        generate=generate_mdl_sql or generate_mdl_sql_once,
    )


def preflight_mdl_sql(
    spec: QuerySpec,
    *,
    plan_id: str = "",
    kind: str | None = None,
    registry: CapabilityRegistry | None = None,
    mdl_snapshot: MdlSnapshot | None = None,
) -> MdlSqlPipelineResult | None:
    registry = registry or CapabilityRegistry.load()
    resolved_kind = str(kind or spec.kind or "mysql")
    snapshot = mdl_snapshot or load_mdl_snapshot(resolved_kind)

    contract = _build_contract(spec, registry, snapshot, plan_id)
    context = _build_context(
        contract,
        registry,
        snapshot,
        kind=resolved_kind,
    )
    subgraph = _build_subgraph(contract, snapshot, registry)
    policy = registry.snapshot.mdl_compilation

    deterministic = compile_contract_to_mdl_sql(contract, context)
    deterministic_failure = deterministic.failure
    if deterministic_failure is not None:
        if deterministic_failure.code == "compiler_not_covered":
            return None
        raise MdlSqlPipelineError(deterministic_failure)
    if not deterministic.mdl_sql.strip():
        return None

    audit = audit_mdl_sql(
        deterministic,
        contract,
        subgraph,
        snapshot,
        policy,
    )
    if not audit.accepted:
        raise MdlSqlPipelineError(
            _audit_failure(
                contract,
                deterministic,
                audit,
            )
        )

    dry_plan = dry_plan_candidate(
        resolved_kind,
        deterministic,
        snapshot,
    )
    if not dry_plan.accepted:
        raise MdlSqlPipelineError(
            _dry_plan_failure(
                contract,
                deterministic,
                dry_plan,
            )
        )

    return MdlSqlPipelineResult(
        contract=contract,
        context=context,
        relationship_subgraph=subgraph,
        candidate=deterministic,
        audit=audit,
        dry_plan=dry_plan,
        physical_sql=dry_plan.physical_sql,
    )


def compile_queryspec_to_physical(
    spec: QuerySpec,
    *,
    plan_id: str = "",
) -> str:
    result = run_mdl_sql_pipeline(spec, plan_id=plan_id)
    physical_sql = str(result.physical_sql or "").strip()
    if not physical_sql:
        raise MdlSqlPipelineError(
            _failure(
                result.contract,
                code="physical_sql_invalid",
                stage="physical",
                reason="pipeline_returned_empty_physical_sql",
                mdl_sql=result.candidate.mdl_sql,
            )
        )
    return physical_sql


def compile_queryspec_to_mdl_sql(
    spec: QuerySpec,
    *,
    plan_id: str = "",
    registry: CapabilityRegistry | None = None,
    mdl_snapshot: MdlSnapshot | None = None,
) -> str | None:
    registry = registry or CapabilityRegistry.load()
    resolved_kind = str(spec.kind or "mysql")
    snapshot = mdl_snapshot or load_mdl_snapshot(resolved_kind)
    contract = _build_contract(spec, registry, snapshot, plan_id)
    context = _build_context(
        contract,
        registry,
        snapshot,
        kind=resolved_kind,
    )
    candidate = compile_contract_to_mdl_sql(contract, context)
    if candidate.failure is not None:
        if candidate.failure.code == "compiler_not_covered":
            return None
        raise MdlSqlPipelineError(candidate.failure)
    mdl_sql = str(candidate.mdl_sql or "").strip()
    return mdl_sql or None


def _run_fallback(
    *,
    contract: MdlQueryContract,
    context: PromptContextBundle,
    subgraph: RelationshipSubgraph,
    snapshot: MdlSnapshot,
    kind: str,
    policy: Any,
    deterministic_failure: CompilerFailure,
    generate: MdlSqlGenerator,
) -> MdlSqlPipelineResult:
    try:
        candidate = generate(context, deterministic_failure)
    except MdlSqlFallbackError as exc:
        raise MdlSqlPipelineError(
            _failure(
                contract,
                code=exc.code,
                stage="fallback",
                reason=str(exc),
                evidence=exc.evidence,
                mdl_sql="",
            )
        ) from exc

    _require_candidate_contract(candidate, contract)
    audit = audit_mdl_sql(
        candidate,
        contract,
        subgraph,
        snapshot,
        policy,
    )
    if not audit.accepted:
        raise MdlSqlPipelineError(
            _audit_failure(contract, candidate, audit)
        )

    _require_declared_scope(candidate, audit)
    dry_plan = dry_plan_candidate(kind, candidate, snapshot)
    if not dry_plan.accepted:
        raise MdlSqlPipelineError(
            _dry_plan_failure(contract, candidate, dry_plan)
        )
    return MdlSqlPipelineResult(
        contract=contract,
        context=context,
        relationship_subgraph=subgraph,
        candidate=candidate,
        audit=audit,
        dry_plan=dry_plan,
        physical_sql=dry_plan.physical_sql,
        deterministic_failure=deterministic_failure,
    )


def _build_contract(
    spec: QuerySpec,
    registry: CapabilityRegistry,
    snapshot: MdlSnapshot,
    plan_id: str,
) -> MdlQueryContract:
    try:
        return build_mdl_query_contract(
            spec,
            registry,
            snapshot,
            plan_id=plan_id,
        )
    except MdlSqlRelationshipError as exc:
        raise MdlSqlPipelineError(
            CompilerFailure(
                code=exc.code or "relationship_path_ambiguous",
                stage="relationship",
                evidence=exc.evidence,
            )
        ) from exc
    except MdlSqlContractError as exc:
        raise MdlSqlPipelineError(
            CompilerFailure(
                code=exc.code or "contract_build_failed",
                stage="contract",
                evidence=exc.evidence,
            )
        ) from exc


def _build_context(
    contract: MdlQueryContract,
    registry: CapabilityRegistry,
    snapshot: MdlSnapshot,
    *,
    kind: str,
) -> PromptContextBundle:
    try:
        context = build_prompt_context(
            contract,
            registry,
            snapshot,
            kind=kind,
        )
        serialize_prompt_context(
            context,
            max_context_bytes=(
                registry.snapshot.mdl_compilation.max_context_bytes
            ),
        )
        return context
    except MdlSqlContextError as exc:
        raise MdlSqlPipelineError(
            CompilerFailure(
                code=exc.code or "context_budget_exceeded",
                stage="context",
                evidence=exc.evidence,
                contract_id=contract.contract_id,
                contract_hash=contract.contract_hash,
            )
        ) from exc


def _build_subgraph(
    contract: MdlQueryContract,
    snapshot: MdlSnapshot,
    registry: CapabilityRegistry,
) -> RelationshipSubgraph:
    try:
        return build_relationship_subgraph(
            snapshot,
            contract.root_model,
            contract.allowed_models,
            contract.required_relationship_ids,
            registry.snapshot.mdl_compilation.max_relationship_hops,
        )
    except MdlSqlRelationshipError as exc:
        raise MdlSqlPipelineError(
            CompilerFailure(
                code=exc.code or "relationship_path_ambiguous",
                stage="relationship",
                evidence=exc.evidence,
                contract_id=contract.contract_id,
                contract_hash=contract.contract_hash,
            )
        ) from exc


def _audit_failure(
    contract: MdlQueryContract,
    candidate: MdlSqlCandidate,
    audit: SqlAuditReport,
) -> CompilerFailure:
    first = audit.issues[0] if audit.issues else None
    code = first.code if first is not None else "invalid_view_shape"
    return _failure(
        contract,
        code=code,
        stage="audit",
        reason=first.message if first is not None else "audit_rejected_sql",
        evidence={
            "issues": tuple(
                {
                    "code": issue.code,
                    "message": issue.message,
                    "path": issue.path,
                    "evidence": dict(issue.evidence),
                }
                for issue in audit.issues
            )
        },
        mdl_sql=candidate.mdl_sql,
    )


def _dry_plan_failure(
    contract: MdlQueryContract,
    candidate: MdlSqlCandidate,
    result: DryPlanResult,
) -> CompilerFailure:
    failure_code = str(
        result.evidence.get("failure_code")
        or "dry_plan_failed"
    )
    stage = (
        "physical"
        if failure_code == "physical_sql_invalid"
        else "dry_plan"
    )
    return _failure(
        contract,
        code=failure_code,
        stage=stage,
        reason=str(result.error or "dry_plan_rejected_candidate"),
        evidence=result.evidence,
        mdl_sql=candidate.mdl_sql,
        physical_sql=result.physical_sql,
    )


def _require_candidate_contract(
    candidate: MdlSqlCandidate,
    contract: MdlQueryContract,
) -> None:
    if candidate.contract_id and candidate.contract_id != contract.contract_id:
        raise MdlSqlPipelineError(
            _failure(
                contract,
                code="llm_fallback_failed",
                stage="fallback",
                reason="fallback_contract_id_mismatch",
                mdl_sql=candidate.mdl_sql,
            )
        )
    if (
        candidate.contract_hash
        and candidate.contract_hash != contract.contract_hash
    ):
        raise MdlSqlPipelineError(
            _failure(
                contract,
                code="llm_fallback_failed",
                stage="fallback",
                reason="fallback_contract_hash_mismatch",
                mdl_sql=candidate.mdl_sql,
            )
        )


def _require_declared_scope(
    candidate: MdlSqlCandidate,
    audit: SqlAuditReport,
) -> None:
    declared_models = tuple(sorted(set(candidate.used_models)))
    audited_models = tuple(sorted(set(audit.used_models)))
    declared_relationships = tuple(
        sorted(set(candidate.used_relationship_ids))
    )
    audited_relationships = tuple(
        sorted(set(audit.used_relationships))
    )
    if (
        declared_models == audited_models
        and declared_relationships == audited_relationships
    ):
        return
    raise MdlSqlPipelineError(
        CompilerFailure(
            code="llm_declared_scope_mismatch",
            stage="audit",
            evidence={
                "reason": "declared_scope_differs_from_sql_ast",
                "declared_models": declared_models,
                "audited_models": audited_models,
                "declared_relationship_ids": declared_relationships,
                "audited_relationship_ids": audited_relationships,
                "contract_id": candidate.contract_id,
                "contract_hash": candidate.contract_hash,
            },
            contract_id=candidate.contract_id,
            contract_hash=candidate.contract_hash,
            mdl_sql=candidate.mdl_sql,
        )
    )


def _failure(
    contract: MdlQueryContract,
    *,
    code: str,
    stage: str,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
    mdl_sql: str = "",
    physical_sql: str = "",
) -> CompilerFailure:
    return CompilerFailure(
        code=code,
        stage=stage,
        evidence={
            "reason": reason,
            **dict(evidence or {}),
            "contract_id": contract.contract_id,
            "contract_hash": contract.contract_hash,
            "subject": contract.subject,
            "root_model": contract.root_model,
            "view": contract.view,
        },
        contract_id=contract.contract_id,
        contract_hash=contract.contract_hash,
        mdl_sql=mdl_sql,
        physical_sql=physical_sql,
    )
