from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable

from .compiler import CompiledQuery
from .contracts import (
    ExecutionObservation,
    QuerySpec,
    VerificationIssue,
    VerificationReport,
)


SemanticChecker = Callable[
    [QuerySpec, CompiledQuery, ExecutionObservation],
    Iterable[Any],
]
PresentationChecker = Callable[
    [QuerySpec, VerificationReport],
    Iterable[Any],
]


def _issue(item: Any, *, stage: str, node_id: str) -> VerificationIssue:
    if isinstance(item, VerificationIssue):
        return item
    return VerificationIssue(
        code=str(item or "verification_issue"),
        message=str(item or "验收失败"),
        node_id=node_id,
        stage=stage,
    )


def verify_deterministic(
    spec: QuerySpec,
    compiled: CompiledQuery,
    observation: ExecutionObservation,
) -> tuple[VerificationIssue, ...]:
    issues: list[VerificationIssue] = []
    if not observation.ok:
        issues.append(
            VerificationIssue(
                code="execution_failed",
                message=observation.runtime_error or "SQL 执行失败",
                node_id=spec.id,
                stage="execution",
            )
        )
        return tuple(issues)
    if observation.rows_fetched == 0:
        issues.append(
            VerificationIssue(
                code="empty_result",
                message="查询没有返回数据",
                node_id=spec.id,
                stage="fact",
            )
        )
    if (
        observation.total_count is not None
        and observation.total_count > observation.rows_fetched
        and not observation.truncated
    ):
        issues.append(
            VerificationIssue(
                code="truncation_not_reported",
                message="命中总数大于实际取回行数，但未标记截断",
                node_id=spec.id,
                stage="fact",
            )
        )
    if observation.total_count is not None and observation.rows_fetched > observation.total_count:
        issues.append(
            VerificationIssue(
                code="row_count_exceeds_total",
                message="实际取回行数大于命中总数",
                node_id=spec.id,
                stage="fact",
            )
        )
    if spec.view in {"detail", "list"}:
        if spec.output_projection and not observation.columns:
            issues.append(
                VerificationIssue(
                    code="missing_output_columns",
                    message="明细查询缺少展示字段",
                    node_id=spec.id,
                    stage="fact",
                )
            )
    elif spec.view in {"distribution", "trend", "count", "sum", "avg", "top"}:
        numeric = any(
            isinstance(value, (int, float))
            or (
                isinstance(value, str)
                and value.replace(".", "", 1).replace("-", "", 1).isdigit()
            )
            for row in observation.preview_rows[:10]
            for value in row.values()
        )
        if not numeric:
            issues.append(
                VerificationIssue(
                    code="missing_numeric_metric",
                    message="查询结果缺少可解析的数值指标",
                    node_id=spec.id,
                    stage="fact",
                )
            )
    return tuple(issues)


def verify_presentation(issues: Iterable[Any]) -> VerificationReport:
    normalized = tuple(
        _issue(item, stage="presentation", node_id="")
        for item in issues
    )
    if not normalized:
        return VerificationReport(status="accepted")
    return VerificationReport(
        status="repairable",
        issues=normalized,
        allowed_actions=("repair_presentation",),
    )


def verify_result(
    spec: QuerySpec,
    compiled: CompiledQuery,
    observation: ExecutionObservation,
    semantic_checker: SemanticChecker | None = None,
    presentation_checker: PresentationChecker | None = None,
) -> VerificationReport:
    deterministic = verify_deterministic(spec, compiled, observation)
    if deterministic:
        return VerificationReport(
            status="repairable",
            issues=deterministic,
            allowed_actions=("patch_queryspec", "retry_compile"),
        )
    fact_report = VerificationReport(status="accepted")
    if spec.view not in {"detail", "list"} and semantic_checker is not None:
        semantic = tuple(
            _issue(item, stage="semantic", node_id=spec.id)
            for item in semantic_checker(
                spec,
                compiled,
                ExecutionObservation(
                    **{
                        **observation.__dict__,
                        "preview_rows": observation.preview_rows[:10],
                    }
                ),
            )
        )
        if semantic:
            return VerificationReport(
                status="repairable",
                issues=semantic,
                allowed_actions=("patch_queryspec", "retry_compile"),
            )
    if presentation_checker is not None:
        return verify_presentation(presentation_checker(spec, fact_report))
    return fact_report
