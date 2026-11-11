from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import sqlglot
from sqlglot import exp

from ..models import MdlFilterBinding


def predicate_atoms(
    expression: exp.Expression,
) -> Iterable[exp.Expression]:
    if isinstance(expression, exp.Paren):
        yield from predicate_atoms(expression.this)
        return
    if isinstance(expression, exp.And):
        yield from predicate_atoms(expression.this)
        yield from predicate_atoms(expression.expression)
        return
    if isinstance(expression, exp.Or):
        yield from predicate_atoms(expression.this)
        yield from predicate_atoms(expression.expression)
        return
    yield expression


def filter_match_status(
    expression: exp.Expression,
    expected: MdlFilterBinding,
    *,
    aliases: Mapping[str, str],
) -> str:
    actual = predicate_operand_parts(expression)
    if actual is None:
        return "none"
    operand, op, value = actual
    if not _operand_matches(operand, expected, aliases):
        return "none"
    expected_op = normalize_filter_operator(
        expected.op,
        expected.value,
    )
    actual_op = normalize_filter_operator(op, value)
    if actual_op != expected_op:
        return "none"
    return (
        "match"
        if values_equal(value, expected.value)
        else "mismatch"
    )


def filter_part_matches(
    expression: exp.Expression,
    expected: MdlFilterBinding,
    *,
    op: str,
    value: Any,
    aliases: Mapping[str, str],
) -> bool:
    parts = predicate_operand_parts(expression)
    if parts is None:
        return False
    operand, actual_op, actual_value = parts
    return (
        _operand_matches(operand, expected, aliases)
        and actual_op == op
        and values_equal(actual_value, value)
    )


def predicate_parts(
    expression: exp.Expression,
) -> tuple[exp.Column, str, Any] | None:
    parts = predicate_operand_parts(expression)
    if parts is None or not isinstance(parts[0], exp.Column):
        return None
    return parts


def predicate_operand_parts(
    expression: exp.Expression,
) -> tuple[exp.Expression, str, Any] | None:
    operators: tuple[tuple[type[exp.Expression], str], ...] = (
        (exp.EQ, "="),
        (exp.NEQ, "!="),
        (exp.GT, ">"),
        (exp.GTE, ">="),
        (exp.LT, "<"),
        (exp.LTE, "<="),
    )
    for operator_type, name in operators:
        if isinstance(expression, operator_type):
            if not _is_literal(expression.this):
                return (
                    expression.this,
                    name,
                    literal_value(expression.expression),
                )
            if not _is_literal(expression.expression):
                return (
                    expression.expression,
                    reverse_operator(name),
                    literal_value(expression.this),
                )
    if isinstance(expression, exp.In) and not _is_literal(expression.this):
        return (
            expression.this,
            "in",
            tuple(
                literal_value(item)
                for item in expression.expressions
            ),
        )
    if isinstance(expression, exp.Between) and not _is_literal(
        expression.this
    ):
        return (
            expression.this,
            "between",
            (
                literal_value(expression.args.get("low")),
                literal_value(expression.args.get("high")),
            ),
        )
    if isinstance(expression, exp.Is) and not _is_literal(
        expression.this
    ):
        if isinstance(expression.expression, exp.Null):
            return expression.this, "is_null", None
    if isinstance(expression, exp.Not):
        inner = expression.this
        if (
            isinstance(inner, exp.Is)
            and not _is_literal(inner.this)
            and isinstance(inner.expression, exp.Null)
        ):
            return inner.this, "not_null", None
    return None


def _is_literal(expression: exp.Expression | None) -> bool:
    return isinstance(
        expression,
        (exp.Literal, exp.Null, exp.Boolean, exp.Placeholder),
    )


def _operand_matches(
    actual: exp.Expression,
    expected: MdlFilterBinding,
    aliases: Mapping[str, str],
) -> bool:
    if isinstance(actual, exp.Column):
        qualifier = str(actual.table or "")
        model = aliases.get(qualifier, qualifier) if qualifier else ""
        if (model, actual.name) == (expected.model, expected.column):
            return True
    if not str(expected.expression or "").strip():
        return False
    try:
        expected_expression = sqlglot.parse_one(
            str(expected.expression),
            read="mysql",
        )
    except sqlglot.errors.ParseError:
        return False
    if expected_expression is None:
        return False
    return _canonical_expression_sql(
        actual,
        aliases,
    ) == _canonical_expression_sql(
        expected_expression,
        aliases,
    )


def _canonical_expression_sql(
    expression: exp.Expression,
    aliases: Mapping[str, str],
) -> str:
    normalized = expression.copy()
    for column in normalized.find_all(exp.Column):
        qualifier = str(column.table or "")
        model = aliases.get(qualifier, qualifier)
        if model:
            column.set("table", exp.to_identifier(model))
    return normalized.sql(dialect="mysql")


def reverse_operator(value: str) -> str:
    return {
        ">": "<",
        ">=": "<=",
        "<": ">",
        "<=": ">=",
    }.get(value, value)


def predicate_value(expression: exp.Expression) -> Any:
    parts = predicate_parts(expression)
    return parts[2] if parts else None


def literal_value(expression: exp.Expression | None) -> Any:
    if expression is None:
        return None
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    if isinstance(expression, exp.Null):
        return None
    return expression.sql()


def normalize_filter_operator(
    op: object,
    value: Any,
) -> str:
    operator = str(op or "").lower()
    if operator == "=" and isinstance(value, (list, tuple)):
        return "in"
    if operator == "empty":
        return "="
    if operator == "not_empty":
        return "!="
    return operator


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, tuple) or isinstance(right, tuple):
        return tuple(left or ()) == tuple(right or ())
    return left == right
