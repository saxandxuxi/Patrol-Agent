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
from .empty_results import aggregate_result_is_empty
from .result_projection import result_field_map


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
    *,
    scope_has_records: bool = False,
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
    bounded_empty = (
        observation.rows_fetched == 0
        and bool(spec.filters)
        and spec.view in {
            "distribution",
            "trend",
            "top_groups",
            "top_records",
            "list",
            "detail",
        }
    )
    # 台账里这个范围本来就有数据（覆盖已核对过），只是这次筛选范围内没有记录：
    # 既包括 0 行明细，也包括聚合查询返回 1 行 0/NULL（COUNT 无匹配、
    # SUM/AVG 无有效数值）。那是“0 条/无有效数值”这个事实，不是指标损坏，
    # 不能拦住依赖它的步骤。
    scoped_empty = bool(spec.filters) and scope_has_records
    if observation.rows_fetched == 0 and not (bounded_empty or scoped_empty):
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
    if spec.view in {"detail", "list", "top_records"}:
        if spec.output_projection and not observation.columns:
            if observation.rows_fetched > 0:
                issues.append(
                    VerificationIssue(
                        code="missing_output_columns",
                        message="明细查询缺少展示字段",
                        node_id=spec.id,
                        stage="fact",
                    )
                )
    elif spec.view in {
        "distribution",
        "trend",
        "count",
        "sum",
        "avg",
        "top_groups",
    }:
        if spec.metrics and observation.rows_fetched > 0:
            observed_columns = observation.columns or next(
                (
                    tuple(row.keys())
                    for row in observation.preview_rows[:1]
                    if isinstance(row, dict)
                ),
                (),
            )
            field_map = result_field_map(spec, observed_columns)
            missing_metrics = [
                str(metric)
                for metric in spec.metrics
                if str(metric).rsplit(".", 1)[-1] not in field_map
            ]
            if missing_metrics:
                issues.append(
                    VerificationIssue(
                        code="missing_metric_columns",
                        message=(
                            "查询结果缺少请求的指标列："
                            + "、".join(missing_metrics)
                        ),
                        node_id=spec.id,
                        stage="fact",
                        evidence={"missing_metrics": missing_metrics},
                    )
                )
        label_columns = [
            key
            for key in observation.columns
            if not any(
                str(row.get(key, ""))
                .replace(".", "", 1)
                .replace("-", "", 1)
                .isdigit()
                for row in observation.preview_rows[:10]
            )
        ]
        if (
            spec.view == "distribution"
            and observation.rows_fetched > 0
            and label_columns
        ):
            has_label = any(
                str(row.get(key) or "").strip().upper()
                not in {"", "NULL", "NONE"}
                for row in observation.preview_rows[:10]
                for key in label_columns
            )
            if not has_label:
                issues.append(
                    VerificationIssue(
                        code="unusable_group_label",
                        message="分组标签全部为空或 NULL",
                        node_id=spec.id,
                        stage="fact",
                    )
                )
        numeric = any(
            isinstance(value, (int, float))
            or (
                isinstance(value, str)
                and value.replace(".", "", 1).replace("-", "", 1).isdigit()
            )
            for row in observation.preview_rows[:10]
            for value in row.values()
        )
        aggregate_empty = aggregate_result_is_empty(spec, observation)
        if (
            observation.rows_fetched > 0
            and not numeric
            and not scoped_empty
            and not aggregate_empty
        ):
            null_like = any(
                str(value or "").strip().upper() in {"", "NULL", "NONE"}
                for row in observation.preview_rows[:10]
                for value in row.values()
            )
            issues.append(
                VerificationIssue(
                    code="missing_numeric_metric",
                    message=(
                        "查询结果里这个指标全部为空（该范围内没有可统计的数值）"
                        if null_like
                        else "查询结果缺少可解析的数值指标"
                    ),
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
    *,
    scope_has_records: bool = False,
) -> VerificationReport:
    deterministic = verify_deterministic(
        spec,
        compiled,
        observation,
        scope_has_records=scope_has_records,
    )
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
