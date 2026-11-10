from __future__ import annotations

from types import MappingProxyType

import sqlglot
from sqlglot import exp

from ..models import ParsedMdlSql
from .failures import sql_issue


_DIALECTS = {
    "mysql": "mysql",
    "postgres": "postgres",
}

_STATEMENT_EXPRESSIONS = tuple(
    expression_type
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Alter",
        "Drop",
        "TruncateTable",
        "Grant",
        "Revoke",
        "Command",
    )
    if isinstance(
        expression_type := getattr(exp, name, None),
        type,
    )
)


def parse_mdl_sql(sql: str, kind: str) -> ParsedMdlSql:
    dialect = _DIALECTS.get(str(kind or "").lower(), "")
    text = str(sql or "")
    if not dialect or not text.strip():
        return _invalid_parse(
            text,
            dialect or str(kind or ""),
            "dialect_or_sql_empty",
        )
    try:
        statements = sqlglot.parse(text, read=dialect)
    except sqlglot.errors.ParseError as exc:
        return _invalid_parse(
            text,
            dialect,
            "sqlglot_parse_error",
            error=str(exc),
        )
    if len(statements) > 1:
        return _invalid_parse(
            text,
            dialect,
            "multiple_statements",
            issue_code="multiple_statements",
            statement_count=len(statements),
        )
    if not statements:
        return _invalid_parse(
            text,
            dialect,
            "empty_statement",
        )

    expression = statements[0]
    root_type = str(expression.key or type(expression).__name__).lower()
    if not _is_read_only_query(expression):
        if not _is_recognized_sql_statement(expression):
            return _invalid_parse(
                text,
                dialect,
                "invalid_root_node",
                root_query_type=root_type,
            )
        issue = sql_issue(
            "forbidden_sql_type",
            path="$",
            evidence={"root_query_type": root_type},
        )
        return ParsedMdlSql(
            sql=text,
            dialect=dialect,
            valid=False,
            root_query_type=root_type,
            normalized_sql=expression.sql(dialect=dialect),
            issues=(issue,),
        )

    table_aliases: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        name = str(table.name or "")
        alias = str(table.alias_or_name or name)
        if name and alias:
            table_aliases[alias] = name

    qualified_columns = tuple(
        dict.fromkeys(
            f"{column.table}.{column.name}"
            for column in expression.find_all(exp.Column)
            if column.table and column.name
        )
    )
    joins = tuple(
        join.sql(dialect=dialect)
        for join in expression.find_all(exp.Join)
    )
    projections = tuple(
        item.sql(dialect=dialect)
        for item in expression.expressions
    )
    group_expressions = _clause_expressions(
        expression,
        "group",
        dialect,
    )
    order_expressions = _clause_expressions(
        expression,
        "order",
        dialect,
    )
    return ParsedMdlSql(
        sql=text,
        dialect=dialect,
        valid=True,
        root_query_type=root_type,
        table_aliases=MappingProxyType(table_aliases),
        qualified_columns=qualified_columns,
        joins=joins,
        projections=projections,
        group_expressions=group_expressions,
        order_expressions=order_expressions,
        limit=_limit_value(expression),
        normalized_sql=expression.sql(dialect=dialect),
        issues=(),
    )


def _is_read_only_query(expression: exp.Expression) -> bool:
    query = expression
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.Select):
        return not bool(query.args.get("into"))
    if isinstance(query, exp.SetOperation):
        return _is_read_only_query(query.left) and _is_read_only_query(
            query.right
        )
    return False


def _is_recognized_sql_statement(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.Query):
        return True
    return isinstance(expression, _STATEMENT_EXPRESSIONS)


def _clause_expressions(
    expression: exp.Select,
    name: str,
    dialect: str,
) -> tuple[str, ...]:
    clause = expression.args.get(name)
    values = getattr(clause, "expressions", ()) if clause else ()
    return tuple(item.sql(dialect=dialect) for item in values)


def _limit_value(expression: exp.Select) -> int | None:
    limit = expression.args.get("limit")
    value = getattr(limit, "expression", None) if limit else None
    if isinstance(value, exp.Literal):
        try:
            return int(value.this)
        except (TypeError, ValueError):
            return None
    return None


def _invalid_parse(
    text: str,
    dialect: str,
    reason: str,
    *,
    issue_code: str = "sql_parse_failed",
    **evidence: object,
) -> ParsedMdlSql:
    return ParsedMdlSql(
        sql=text,
        dialect=dialect,
        valid=False,
        issues=(
            sql_issue(
                issue_code,
                path="$",
                evidence={"reason": reason, **evidence},
            ),
        ),
    )
