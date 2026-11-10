from __future__ import annotations

from collections.abc import Mapping

from sqlglot import exp

from ..models import MdlMetricBinding, MdlQueryContract, SqlIssue
from .ast_facts import (
    canonical_expression,
    clause_expressions,
    contains_aggregate,
    parse_expression,
    projections,
    resolve_projection_alias,
    root_select,
)
from .failures import sql_issue
from .ranking_audit import has_rank_threshold, rank_alias


_GROUPED_VIEWS = frozenset({"distribution", "trend", "top_groups"})
_RECORD_VIEWS = frozenset({"list", "detail", "data", "top_records"})


def audit_view_shape(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    dialect: str,
) -> tuple[SqlIssue, ...]:
    select = root_select(expression)
    issues: list[SqlIssue] = []
    items = projections(select)
    for output in contract.outputs:
        actual = items.get(output.alias)
        expected_key = canonical_expression(
            parse_expression(output.expression, dialect),
            aliases=aliases,
            contract=contract,
            dialect=dialect,
        )
        if actual is None:
            issues.append(
                sql_issue(
                    "missing_output",
                    path="$.projections",
                    evidence={
                        "output": output.output_id,
                        "alias": output.alias,
                        "expression": output.expression,
                    },
                )
            )
            continue
        actual_key = canonical_expression(
            actual,
            aliases=aliases,
            contract=contract,
            dialect=dialect,
        )
        if (
            isinstance(actual, exp.Column)
            and not actual.table
            and actual.name == output.alias
        ):
            actual_key = expected_key
        if actual_key != expected_key:
            issues.append(
                sql_issue(
                    "missing_output",
                    path="$.projections",
                    evidence={
                        "output": output.output_id,
                        "alias": output.alias,
                        "expected_expression": output.expression,
                        "actual_expression": actual.sql(dialect=dialect),
                    },
                )
            )

    if contract.view in _GROUPED_VIEWS:
        issues.extend(
            _group_shape_issues(
                expression,
                select=select,
                aliases=aliases,
                contract=contract,
                dialect=dialect,
            )
        )
    for metric in contract.metrics:
        issues.extend(
            _metric_shape_issues(
                expression,
                items=items,
                aliases=aliases,
                contract=contract,
                metric=metric,
                dialect=dialect,
            )
        )
    if contract.view in _RECORD_VIEWS and any(
        contains_aggregate(item) for item in items.values()
    ):
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.projections",
                evidence={
                    "view": contract.view,
                    "reason": "detail_view_was_aggregated",
                },
            )
        )
    return tuple(issues)


def _group_shape_issues(
    expression: exp.Expression,
    *,
    select: exp.Select,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    dialect: str,
) -> tuple[SqlIssue, ...]:
    expected_group = tuple(
        sorted(
            canonical_expression(
                parse_expression(value, dialect),
                aliases=aliases,
                contract=contract,
                dialect=dialect,
            )
            for value in contract.group_by
        )
    )
    grouped_values = tuple(
        tuple(
            sorted(
                canonical_expression(
                    item,
                    aliases=aliases,
                    contract=contract,
                    dialect=dialect,
                )
                for item in clause_expressions(select_item, "group")
            )
        )
        for select_item in expression.find_all(exp.Select)
    )
    actual_group = next(
        (
            value
            for value in grouped_values
            if value == expected_group
        ),
        grouped_values[0] if grouped_values else (),
    )
    issues: list[SqlIssue] = []
    if not actual_group or actual_group != expected_group:
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.group",
                evidence={
                    "view": contract.view,
                    "expected": list(expected_group),
                    "actual": list(actual_group),
                },
            )
        )
    names = rank_alias(expression)
    if (
        "top_per_outer_group" in contract.required_assertions
        and (
            not names
            or not has_rank_threshold(
                select,
                rank_alias=names,
                limit=contract.limit,
            )
        )
    ):
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.projections",
                evidence={
                    "view": contract.view,
                    "expected_shape": "window_rank",
                },
            )
        )
    return tuple(issues)


def _metric_shape_issues(
    expression: exp.Expression,
    *,
    items: Mapping[str, exp.Expression],
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    metric: MdlMetricBinding,
    dialect: str,
) -> tuple[SqlIssue, ...]:
    output_alias = _metric_output_alias(
        contract,
        metric.metric_id,
    )
    projection = resolve_projection_alias(
        expression,
        items,
        output_alias,
    )
    if projection is None:
        return ()
    expected_key = canonical_expression(
        parse_expression(metric.expression, dialect),
        aliases=aliases,
        contract=contract,
        dialect=dialect,
    )
    actual_key = canonical_expression(
        projection,
        aliases=aliases,
        contract=contract,
        dialect=dialect,
    )
    issues: list[SqlIssue] = []
    if actual_key != expected_key:
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.projections",
                evidence={
                    "metric": metric.metric_id,
                    "expected_expression": metric.expression,
                    "actual_expression": projection.sql(dialect=dialect),
                },
            )
        )
    if not contains_aggregate(projection):
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.projections",
                evidence={
                    "metric": metric.metric_id,
                    "expected_expression": metric.expression,
                    "reason": "aggregate_metric_missing",
                },
            )
        )
    return tuple(issues)


def _metric_output_alias(
    contract: MdlQueryContract,
    metric_id: str,
) -> str:
    for output in contract.outputs:
        if output.output_id == metric_id:
            return output.alias
    return metric_id
