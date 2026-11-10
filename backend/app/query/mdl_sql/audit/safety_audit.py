from __future__ import annotations

from collections.abc import Mapping

import sqlglot
from sqlglot import exp

from ...capability_registry.models import MdlCompilationPolicy
from ..models import (
    MdlQueryContract,
    MdlSnapshot,
    ParsedMdlSql,
    SqlIssue,
)
from .failures import sql_issue


def audit_safety(
    parsed: ParsedMdlSql,
    contract: MdlQueryContract,
    snapshot: MdlSnapshot,
    policy: MdlCompilationPolicy,
) -> tuple[SqlIssue, ...]:
    issues = list(parsed.issues)
    if not parsed.valid:
        return tuple(issues)

    expression = sqlglot.parse_one(
        parsed.normalized_sql,
        read=parsed.dialect,
    )
    cte_names = {
        str(cte.alias_or_name)
        for cte in expression.find_all(exp.CTE)
        if str(cte.alias_or_name)
    }
    for index, table in enumerate(expression.find_all(exp.Table)):
        name = str(table.name or "")
        if not name or name in cte_names:
            continue
        if table.db or table.catalog:
            issues.append(
                sql_issue(
                    "physical_table_reference",
                    path=f"$.tables[{index}]",
                    evidence={
                        "table": name,
                        "database": str(table.db or ""),
                        "catalog": str(table.catalog or ""),
                    },
                )
            )
            continue
        if name in snapshot.models:
            continue
        issues.append(
            sql_issue(
                "physical_table_reference",
                path=f"$.tables[{index}]",
                evidence={
                    "table": name,
                    "reason": "table_is_not_an_mdl_model",
                },
            )
        )

    forbidden_functions = {
        str(item).lower() for item in policy.forbidden_functions
    }
    for index, function in enumerate(expression.find_all(exp.Func)):
        name = (
            str(function.name or "").lower()
            if isinstance(function, exp.Anonymous)
            else str(function.sql_name() or "").lower()
        )
        if name in forbidden_functions:
            issues.append(
                sql_issue(
                    "dangerous_function",
                    path=f"$.functions[{index}]",
                    evidence={"function": name},
                )
            )

    projection_index = 0
    for select in expression.find_all(exp.Select):
        for projection in select.expressions:
            if _is_star_projection(projection):
                issues.append(
                    sql_issue(
                        "star_projection",
                        path=f"$.projections[{projection_index}]",
                    )
                )
            projection_index += 1

    alias_sources = {
        alias: str(
            snapshot.physical_table_to_model.get(source, source)
        )
        for alias, source in parsed.table_aliases.items()
    }
    for predicate in contract.mandatory_predicates:
        if not _has_required_predicate(
            expression,
            predicate.model,
            predicate.column,
            predicate.value,
            alias_sources,
            root_model=contract.root_model,
        ):
            issues.append(
                sql_issue(
                    "soft_delete_missing",
                    path="$.where",
                    evidence={
                        "model": predicate.model,
                        "column": predicate.column,
                        "value": predicate.value,
                    },
                )
            )
    return tuple(issues)


def _is_star_projection(projection: exp.Expression) -> bool:
    value = projection.this if isinstance(projection, exp.Alias) else projection
    if isinstance(value, exp.Star):
        return True
    return (
        isinstance(value, exp.Column)
        and isinstance(value.this, exp.Star)
    )


def _has_required_predicate(
    expression: exp.Expression,
    model: str,
    column: str,
    expected: object,
    aliases: Mapping[str, str],
    *,
    root_model: str,
) -> bool:
    for equality in expression.find_all(exp.EQ):
        left = equality.this
        right = equality.expression
        if _column_matches(
            left,
            model,
            column,
            aliases,
            root_model=root_model,
        ) and _literal_matches(right, expected):
            return True
        if _column_matches(
            right,
            model,
            column,
            aliases,
            root_model=root_model,
        ) and _literal_matches(left, expected):
            return True
    return False


def _column_matches(
    expression: exp.Expression,
    model: str,
    column: str,
    aliases: Mapping[str, str],
    *,
    root_model: str,
) -> bool:
    if not isinstance(expression, exp.Column) or expression.name != column:
        return False
    qualifier = str(expression.table or "")
    if qualifier:
        return aliases.get(qualifier, qualifier) == model
    return root_model == model


def _literal_matches(expression: exp.Expression, expected: object) -> bool:
    if not isinstance(expression, exp.Literal):
        return False
    try:
        return expression.to_py() == expected
    except (TypeError, ValueError):
        return False
