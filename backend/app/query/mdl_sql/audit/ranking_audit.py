from __future__ import annotations

from collections.abc import Mapping

from sqlglot import exp

from ..models import MdlQueryContract, SqlIssue
from .ast_facts import (
    canonical_expression,
    clause_expressions,
    limit_value,
    parse_expression,
    projections,
    root_select,
)
from .failures import sql_issue
from .predicate_ast import predicate_atoms, predicate_parts


_ORDER_VIEWS = frozenset({"top_records", "top_groups"})


def audit_order_and_limit(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    dialect: str,
) -> tuple[SqlIssue, ...]:
    select = root_select(expression)
    issues: list[SqlIssue] = []
    actual_orders = tuple(clause_expressions(select, "order"))
    if (
        not actual_orders
        and "top_per_outer_group" in contract.required_assertions
    ):
        actual_orders = window_orders(expression)
    expected_orders = contract.order_by
    if (contract.view in _ORDER_VIEWS) and not expected_orders:
        issues.append(
            sql_issue(
                "order_required",
                path="$.order",
                evidence={"view": contract.view},
            )
        )

    if not expected_orders and actual_orders:
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.order",
                evidence={"reason": "unexpected_order_expression"},
            )
        )

    projected = projections(select)
    matched: set[int] = set()
    for expected in expected_orders:
        expected_key = canonical_expression(
            parse_expression(expected.expression, dialect),
            aliases=aliases,
            contract=contract,
            dialect=dialect,
        )
        match_index = None
        direction_mismatch = False
        for index, ordered in enumerate(actual_orders):
            actual = ordered.this
            if (
                isinstance(actual, exp.Column)
                and not actual.table
                and actual.name in projected
            ):
                actual = projected[actual.name]
            actual_key = canonical_expression(
                actual,
                aliases=aliases,
                contract=contract,
                dialect=dialect,
            )
            if actual_key != expected_key:
                continue
            match_index = index
            actual_direction = (
                "desc"
                if bool(ordered.args.get("desc"))
                else "asc"
            )
            direction_mismatch = (
                actual_direction != expected.direction
            )
            break
        if match_index is None:
            issues.append(
                sql_issue(
                    "order_required",
                    path="$.order",
                    evidence={
                        "field": expected.field_id,
                        "direction": expected.direction,
                    },
                )
            )
            continue
        matched.add(match_index)
        if direction_mismatch:
            issues.append(
                sql_issue(
                    "invalid_view_shape",
                    path="$.order",
                    evidence={
                        "field": expected.field_id,
                        "expected_direction": expected.direction,
                    },
                )
            )

    if expected_orders and len(matched) != len(actual_orders):
        issues.append(
            sql_issue(
                "invalid_view_shape",
                path="$.order",
                evidence={"reason": "unexpected_order_expression"},
            )
        )

    actual_limit = limit_value(select)
    if actual_limit != contract.limit:
        names = rank_alias(expression)
        if (
            "top_per_outer_group" in contract.required_assertions
            and actual_limit is None
            and names
            and has_rank_threshold(
                select,
                rank_alias=names,
                limit=contract.limit,
            )
        ):
            return tuple(issues)
        issues.append(
            sql_issue(
                "invalid_limit",
                path="$.limit",
                evidence={
                    "expected": contract.limit,
                    "actual": actual_limit,
                },
            )
        )
    return tuple(issues)


def rank_alias(expression: exp.Expression) -> str:
    for projection in expression.find_all(exp.Alias):
        value = projection.this
        if not isinstance(value, exp.Window):
            continue
        if isinstance(value.this, exp.RowNumber):
            return str(projection.alias)
    return ""


def window_orders(
    expression: exp.Expression,
) -> tuple[exp.Ordered, ...]:
    values: list[exp.Ordered] = []
    for window in expression.find_all(exp.Window):
        order = window.args.get("order")
        if order is None:
            continue
        values.extend(getattr(order, "expressions", ()))
    return tuple(values)


def has_rank_threshold(
    select: exp.Select,
    *,
    rank_alias: str,
    limit: int | None,
) -> bool:
    where = select.args.get("where")
    if where is None:
        return False
    return any(
        is_rank_threshold(
            atom,
            rank_alias=rank_alias,
            limit=limit,
        )
        for atom in predicate_atoms(where.this)
    )


def is_rank_threshold(
    expression: exp.Expression,
    *,
    rank_alias: str,
    limit: int | None,
) -> bool:
    if limit is None:
        return False
    parts = predicate_parts(expression)
    if parts is None:
        return False
    column, op, value = parts
    if column.table or column.name != rank_alias:
        return False
    return (
        (op == "<=" and value == limit)
        or (op == "<" and value == limit + 1)
    )
