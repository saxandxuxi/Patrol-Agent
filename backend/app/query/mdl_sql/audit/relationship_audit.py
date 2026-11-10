from __future__ import annotations

from collections.abc import Mapping

import sqlglot
from sqlglot import exp

from ..models import (
    MdlQueryContract,
    MdlRelationship,
    ParsedMdlSql,
    RelationshipSubgraph,
    SqlIssue,
)
from .ast_facts import from_table
from .failures import sql_issue


def audit_relationships(
    parsed: ParsedMdlSql,
    contract: MdlQueryContract,
    subgraph: RelationshipSubgraph,
) -> tuple[SqlIssue, ...]:
    issues, _used = relationship_audit_details(
        parsed,
        contract,
        subgraph,
    )
    return issues


def relationship_audit_details(
    parsed: ParsedMdlSql,
    contract: MdlQueryContract,
    subgraph: RelationshipSubgraph,
) -> tuple[tuple[SqlIssue, ...], tuple[str, ...]]:
    issues = list(parsed.issues)
    if not parsed.valid:
        return tuple(issues), ()

    expression = sqlglot.parse_one(
        parsed.normalized_sql,
        read=parsed.dialect,
    )
    aliases = {
        alias: str(name)
        for alias, name in parsed.table_aliases.items()
    }
    cte_names = {
        str(cte.alias_or_name)
        for cte in expression.find_all(exp.CTE)
        if str(cte.alias_or_name)
    }
    used_relationships: list[str] = []

    for select_index, select in enumerate(
        expression.find_all(exp.Select)
    ):
        source = from_table(select)
        source_model = str(source.name) if source is not None else ""
        current_models = (
            {source_model}
            if source_model and source_model not in cte_names
            else set()
        )
        for index, join in enumerate(select.args.get("joins") or ()):
            table = join.this
            path = f"$.selects[{select_index}].joins[{index}]"
            if not isinstance(table, exp.Table):
                issues.append(
                    sql_issue(
                        "illegal_relationship",
                        path=path,
                        evidence={"reason": "join_source_is_not_a_model"},
                    )
                )
                continue
            joined_model = str(table.name or "")
            if joined_model in cte_names:
                continue
            if joined_model not in subgraph.models:
                issues.append(
                    sql_issue(
                        "unexpected_model",
                        path=path,
                        evidence={
                            "model": joined_model,
                            "allowed_models": list(contract.allowed_models),
                        },
                    )
                )
                continue

            on = join.args.get("on")
            if on is None:
                issues.append(
                    sql_issue(
                        "illegal_relationship",
                        path=path,
                        evidence={"reason": "join_without_on_condition"},
                    )
                )
                continue
            actual_pairs = _join_field_pairs(on, aliases)
            if actual_pairs is None:
                issues.append(
                    sql_issue(
                        "illegal_relationship",
                        path=path,
                        evidence={
                            "reason": "join_condition_is_not_field_equality"
                        },
                    )
                )
                continue

            candidates = tuple(
                relationship
                for relationship in subgraph.relationships
                if (
                    joined_model in relationship.models
                    and any(
                        model in current_models
                        for model in relationship.models
                        if model != joined_model
                    )
                )
            )
            matched = tuple(
                relationship
                for relationship in candidates
                if (
                    _relationship_pairs(relationship)
                    == actual_pairs
                )
            )
            if len(matched) != 1:
                issues.append(
                    sql_issue(
                        "illegal_relationship",
                        path=path,
                        evidence={
                            "joined_model": joined_model,
                            "qualified_fields": [
                                f"{model}.{column}"
                                for model, column in sorted(actual_pairs)
                            ],
                            "allowed_relationship_ids": [
                                item.relationship_id
                                for item in subgraph.relationships
                            ],
                        },
                    )
                )
                continue

            relationship = matched[0]
            source_model = next(
                (
                    model
                    for model in relationship.models
                    if model in current_models and model != joined_model
                ),
                "",
            )
            if (
                _is_one_to_many(
                    relationship,
                    source_model=source_model,
                    joined_model=joined_model,
                )
                and not _cardinality_safe(
                    expression,
                    aliases=aliases,
                    source_model=source_model,
                    joined_model=joined_model,
                )
            ):
                issues.append(
                    sql_issue(
                        "cardinality_risk",
                        path=path,
                        evidence={
                            "relationship_id": relationship.relationship_id,
                            "source_model": source_model,
                            "joined_model": joined_model,
                            "cardinality": relationship.join_type,
                        },
                    )
                )
            used_relationships.append(relationship.relationship_id)
            current_models.add(joined_model)

    return tuple(issues), tuple(dict.fromkeys(used_relationships))
def _join_field_pairs(
    expression: exp.Expression,
    aliases: Mapping[str, str],
) -> frozenset[tuple[tuple[str, str], tuple[str, str]]] | None:
    equalities = tuple(_and_equalities(expression))
    if not equalities:
        return None
    result: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for equality in equalities:
        if equality is None:
            return None
        left, right = equality
        result.append(
            (
                _column_ref(left, aliases),
                _column_ref(right, aliases),
            )
        )
    return frozenset(
        tuple(sorted(pair))
        for pair in result
    )


def _and_equalities(
    expression: exp.Expression,
):
    if isinstance(expression, exp.Paren):
        yield from _and_equalities(expression.this)
        return
    if isinstance(expression, exp.And):
        yield from _and_equalities(expression.this)
        yield from _and_equalities(expression.expression)
        return
    if isinstance(expression, exp.EQ):
        if (
            isinstance(expression.this, exp.Column)
            and isinstance(expression.expression, exp.Column)
        ):
            yield expression.this, expression.expression
            return
    yield None


def _column_ref(
    column: exp.Column,
    aliases: Mapping[str, str],
) -> tuple[str, str]:
    qualifier = str(column.table or "")
    model = aliases.get(qualifier, qualifier)
    return model, str(column.name)


def _relationship_pairs(
    relationship: MdlRelationship,
) -> frozenset[tuple[tuple[str, str], tuple[str, str]]]:
    result = []
    for left, right in relationship.field_pairs:
        result.append(
            tuple(
                sorted(
                    (
                        _split_field(left),
                        _split_field(right),
                    )
                )
            )
        )
    return frozenset(result)


def _split_field(value: str) -> tuple[str, str]:
    model, separator, column = str(value).partition(".")
    return model, column if separator else ""


def _is_one_to_many(
    relationship: MdlRelationship,
    *,
    source_model: str,
    joined_model: str,
) -> bool:
    left, right = relationship.models
    cardinality = relationship.join_type.upper()
    if cardinality == "MANY_TO_ONE":
        return source_model == right and joined_model == left
    if cardinality == "ONE_TO_MANY":
        return source_model == left and joined_model == right
    return cardinality == "MANY_TO_MANY"


def _cardinality_safe(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    source_model: str,
    joined_model: str,
) -> bool:
    if expression.find(exp.Exists) is not None:
        return True

    source_primary_key = (
        "id"
        if source_model
        else ""
    )
    for aggregate in expression.find_all(exp.AggFunc):
        columns = {
            _column_ref(column, aliases)
            for column in aggregate.find_all(exp.Column)
        }
        if any(model == joined_model for model, _ in columns):
            return True
        if (
            isinstance(aggregate, exp.Count)
            and (
                bool(aggregate.args.get("distinct"))
                or isinstance(aggregate.this, exp.Distinct)
            )
            and (source_model, source_primary_key) in columns
        ):
            return True
    return False
