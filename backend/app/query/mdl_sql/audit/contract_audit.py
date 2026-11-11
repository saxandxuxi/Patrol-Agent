from __future__ import annotations

from collections.abc import Mapping

import sqlglot
from sqlglot import exp

from ..models import (
    MdlQueryContract,
    ParsedMdlSql,
    RelationshipSubgraph,
    SqlIssue,
)
from .ast_facts import (
    allowed_fields,
    cte_names,
    root_model,
    used_business_fields,
)
from .failures import sql_issue
from .filter_audit import audit_filters
from .ranking_audit import audit_order_and_limit
from .view_audit import audit_view_shape


def audit_contract(
    parsed: ParsedMdlSql,
    contract: MdlQueryContract,
    subgraph: RelationshipSubgraph | None = None,
) -> tuple[SqlIssue, ...]:
    issues = list(parsed.issues)
    if not parsed.valid:
        return tuple(issues)

    expression = sqlglot.parse_one(
        parsed.normalized_sql,
        read=parsed.dialect,
    )
    aliases = {
        alias: str(name)
        for alias, name in parsed.table_aliases.items()
    }
    names = cte_names(expression)
    actual_root = root_model(expression, names)
    if actual_root != contract.root_model:
        issues.append(
            sql_issue(
                "root_subject_mismatch",
                path="$.from",
                evidence={
                    "expected": contract.root_model,
                    "actual": actual_root,
                },
            )
        )

    issues.extend(
        _model_issues(
            expression,
            names=names,
            contract=contract,
        )
    )
    issues.extend(
        _field_issues(
            expression,
            aliases=aliases,
            contract=contract,
            dialect=parsed.dialect,
            relationship_fields=_relationship_fields(subgraph),
        )
    )
    issues.extend(
        audit_filters(
            expression,
            aliases=aliases,
            contract=contract,
            relationships=(
                subgraph.relationships if subgraph is not None else ()
            ),
        )
    )
    issues.extend(
        audit_view_shape(
            expression,
            aliases=aliases,
            contract=contract,
            dialect=parsed.dialect,
        )
    )
    issues.extend(
        audit_order_and_limit(
            expression,
            aliases=aliases,
            contract=contract,
            dialect=parsed.dialect,
        )
    )
    return tuple(issues)


def _relationship_fields(
    subgraph: RelationshipSubgraph | None,
) -> tuple[tuple[str, str], ...]:
    if subgraph is None:
        return ()
    fields: list[tuple[str, str]] = []
    for relationship in subgraph.relationships:
        for pair in relationship.field_pairs:
            for value in pair:
                model, separator, column = str(value).partition(".")
                if separator and model and column:
                    fields.append((model, column))
    return tuple(dict.fromkeys(fields))


def _model_issues(
    expression: exp.Expression,
    *,
    names: set[str],
    contract: MdlQueryContract,
) -> tuple[SqlIssue, ...]:
    issues: list[SqlIssue] = []
    for index, table in enumerate(expression.find_all(exp.Table)):
        name = str(table.name or "")
        if not name or name in names:
            continue
        if name not in contract.allowed_models:
            issues.append(
                sql_issue(
                    "unexpected_model",
                    path=f"$.tables[{index}]",
                    evidence={
                        "model": name,
                        "allowed_models": list(contract.allowed_models),
                    },
                )
            )
    return tuple(issues)


def _field_issues(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    dialect: str,
    relationship_fields: tuple[tuple[str, str], ...],
) -> tuple[SqlIssue, ...]:
    allowed = allowed_fields(
        contract,
        dialect,
        relationship_fields,
    )
    issues: list[SqlIssue] = []
    for field in used_business_fields(
        expression,
        aliases=aliases,
        contract=contract,
    ):
        if field in allowed:
            continue
        issues.append(
            sql_issue(
                "unexpected_field",
                path="$.fields",
                evidence={
                    "field": f"{field[0]}.{field[1]}",
                    "allowed_fields": [
                        f"{model}.{column}"
                        for model, column in sorted(allowed)
                    ],
                },
            )
        )
    return tuple(issues)
