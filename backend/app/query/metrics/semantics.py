from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from ..capability_registry.models import MetricCapability


AGGREGATIONS = frozenset(
    {
        "row",
        "count",
        "count_distinct",
        "sum",
        "avg",
        "min",
        "max",
    }
)
SOURCE_KINDS = frozenset({"resources", "mdl_cube"})


@dataclass(frozen=True)
class MetricSemanticsIssue:
    code: str
    message: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


def validate_metric_semantics(
    metric: MetricCapability,
    *,
    subject: str,
) -> tuple[MetricSemanticsIssue, ...]:
    issues: list[MetricSemanticsIssue] = []
    if not isinstance(metric.fanout_safe, bool):
        issues.append(
            _issue(
                "metric_fanout_safe_must_be_boolean",
                "fanout_safe 必须是布尔值。",
                metric,
                subject,
            )
        )
    if metric.source_kind not in SOURCE_KINDS:
        issues.append(
            _issue(
                "metric_source_kind_invalid",
                "指标来源必须是 resources 或 mdl_cube。",
                metric,
                subject,
            )
        )
    if not metric.source_reference:
        issues.append(
            _issue(
                "metric_source_reference_missing",
                "指标必须登记可追溯的来源引用。",
                metric,
                subject,
            )
        )
    if metric.aggregation not in AGGREGATIONS:
        issues.append(
            _issue(
                "metric_aggregation_invalid",
                "指标聚合方式不在允许集合内。",
                metric,
                subject,
            )
        )
    if not metric.grain:
        issues.append(
            _issue(
                "metric_grain_missing",
                "指标必须登记事实粒度。",
                metric,
                subject,
            )
        )
    if not metric.identity:
        issues.append(
            _issue(
                "metric_identity_missing",
                "指标必须登记粒度身份字段。",
                metric,
                subject,
            )
        )
    elif "." not in metric.identity:
        issues.append(
            _issue(
                "metric_identity_invalid",
                "指标身份字段必须是 model.column 形式。",
                metric,
                subject,
            )
        )

    observed = aggregation_from_expression(metric.expression)
    if (
        metric.aggregation in AGGREGATIONS
        and observed in AGGREGATIONS
        and observed != metric.aggregation
    ):
        issues.append(
            _issue(
                "metric_aggregation_expression_mismatch",
                "指标声明的聚合方式与表达式不一致。",
                metric,
                subject,
                observed_aggregation=observed,
            )
        )

    if (
        metric.aggregation not in {"", "row"}
        and metric.grain
        and metric.grain != subject
        and metric.fanout_safe is False
    ):
        issues.append(
            _issue(
                "metric_requires_own_grain",
                "跨粒度聚合必须先在自己的 grain/identity 上完成。",
                metric,
                subject,
            )
        )
    return tuple(issues)


def aggregation_from_expression(expression: str) -> str:
    try:
        parsed = sqlglot.parse_one(
            f"SELECT {expression}",
            read="mysql",
        )
    except sqlglot.errors.ParseError:
        return ""
    aggregates = [
        item
        for item in parsed.expressions[0].walk()
        if isinstance(item, exp.AggFunc)
    ]
    if not aggregates:
        return "row"
    kinds = {_aggregation_name(item) for item in aggregates}
    if len(kinds) != 1:
        return ""
    return kinds.pop()


def _aggregation_name(value: exp.AggFunc) -> str:
    if isinstance(value, exp.Count):
        return (
            "count_distinct"
            if value.args.get("this") is not None
            and bool(value.args.get("this").find(exp.Distinct))
            else "count"
        )
    if isinstance(value, exp.Sum):
        return "sum"
    if isinstance(value, exp.Avg):
        return "avg"
    if isinstance(value, exp.Min):
        return "min"
    if isinstance(value, exp.Max):
        return "max"
    return ""


def _issue(
    code: str,
    message: str,
    metric: MetricCapability,
    subject: str,
    **extra: Any,
) -> MetricSemanticsIssue:
    return MetricSemanticsIssue(
        code=code,
        message=message,
        evidence={
            "subject": subject,
            "metric_id": metric.metric_id,
            "aggregation": metric.aggregation,
            "grain": metric.grain,
            "identity": metric.identity,
            "fanout_safe": metric.fanout_safe,
            **extra,
        },
    )
