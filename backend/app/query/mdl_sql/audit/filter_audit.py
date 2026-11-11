from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp

from ..models import (
    MdlFilterBinding,
    MdlQueryContract,
    MdlRelationship,
    SqlIssue,
)
from .failures import sql_issue
from .predicate_ast import (
    filter_match_status,
    filter_part_matches,
    predicate_atoms,
    predicate_value,
)
from .ranking_audit import is_rank_threshold, rank_alias


def audit_filters(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    relationships: tuple[MdlRelationship, ...] = (),
) -> tuple[SqlIssue, ...]:
    atoms = tuple(
        atom
        for select in expression.find_all(exp.Select)
        for clause_name in ("where", "having")
        for clause in (select.args.get(clause_name),)
        if clause is not None
        for atom in predicate_atoms(clause.this)
    )
    issues: list[SqlIssue] = []
    matched_atoms: set[int] = set()
    names = (
        rank_alias(expression)
        if "top_per_outer_group" in contract.required_assertions
        else ""
    )
    for required in contract.filters:
        if str(required.op or "").lower() == "exists":
            matches = tuple(
                index
                for index, atom in enumerate(atoms)
                if _is_recognized_exists_filter(
                    atom,
                    aliases=aliases,
                    contract=contract,
                    relationships=relationships,
                    required_filter=required,
                )
            )
            if matches:
                matched_atoms.add(matches[0])
                matched_atoms.update(
                    index
                    for index, atom in enumerate(atoms)
                    if any(
                        atom is inner
                        for select in atoms[matches[0]].find_all(exp.Select)
                        for clause_name in ("where", "having")
                        for clause in (
                            select.args.get(clause_name),
                        )
                        if clause is not None
                        for inner in predicate_atoms(clause.this)
                    )
                )
                continue
            issues.append(
                sql_issue(
                    "missing_filter",
                    path="$.where",
                    evidence={
                        "field": required.field_id,
                        "expression": required.expression,
                        "op": required.op,
                        "value": required.value,
                    },
                )
            )
            continue
        indices, status = filter_match(
            atoms,
            required,
            aliases=aliases,
        )
        if status == "match":
            matched_atoms.update(indices)
            continue
        if status == "mismatch":
            matched_atoms.update(indices)
            issues.append(
                sql_issue(
                    "filter_value_mismatch",
                    path="$.where",
                    evidence={
                        "field": required.field_id,
                        "expected": required.value,
                        "actual": predicate_value(atoms[indices[0]]),
                    },
                )
            )
            continue
        issues.append(
            sql_issue(
                "missing_filter",
                path="$.where",
                evidence={
                    "field": required.field_id,
                    "expression": required.expression,
                    "op": required.op,
                    "value": required.value,
                },
            )
        )

    mandatory = contract.mandatory_predicates
    for index, atom in enumerate(atoms):
        if index in matched_atoms:
            continue
        if any(
            filter_match_status(
                atom,
                item,
                aliases=aliases,
            )
            == "match"
            for item in mandatory
        ):
            continue
        if _is_relationship_equality(
            atom,
            aliases=aliases,
            relationships=relationships,
        ):
            continue
        if _is_recognized_exists_filter(
            atom,
            aliases=aliases,
            contract=contract,
            relationships=relationships,
        ):
            continue
        if (
            names
            and is_rank_threshold(
                atom,
                rank_alias=names,
                limit=contract.limit,
            )
        ):
            continue
        issues.append(
            sql_issue(
                "extra_filter",
                path="$.where",
                evidence={"predicate": atom.sql()},
            )
        )
    return tuple(issues)


def _is_recognized_exists_filter(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    relationships: tuple[MdlRelationship, ...],
    required_filter: MdlFilterBinding | None = None,
) -> bool:
    if not isinstance(expression, exp.Exists):
        return False
    inner_atoms = tuple(
        atom
        for select in expression.find_all(exp.Select)
        for clause_name in ("where", "having")
        for clause in (select.args.get(clause_name),)
        if clause is not None
        for atom in predicate_atoms(clause.this)
    )
    matched_required = False
    for atom in inner_atoms:
        if (
            required_filter is not None
            and _relation_exists_content_matches(
                atom,
                required_filter,
            )
        ):
            matched_required = True
            continue
        if any(
            filter_match_status(atom, item, aliases=aliases) == "match"
            for item in (
                (required_filter,)
                if required_filter is not None
                else contract.filters
            )
        ):
            matched_required = True
            continue
        if any(
            filter_match_status(atom, item, aliases=aliases) == "match"
            for item in contract.mandatory_predicates
        ):
            continue
        if _is_relationship_equality(
            atom,
            aliases=aliases,
            relationships=relationships,
        ):
            continue
        return False
    return matched_required


def _relation_exists_content_matches(
    expression: exp.Expression,
    required: MdlFilterBinding,
) -> bool:
    try:
        expected = sqlglot.parse_one(
            str(required.expression or ""),
            read="mysql",
        )
    except sqlglot.errors.ParseError:
        return False
    return bool(
        expected is not None
        and expression.sql(dialect="mysql")
        == expected.sql(dialect="mysql")
    )


def _is_relationship_equality(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    relationships: tuple[MdlRelationship, ...],
) -> bool:
    if not isinstance(expression, exp.EQ):
        return False
    left = expression.this
    right = expression.expression
    if not (
        isinstance(left, exp.Column)
        and isinstance(right, exp.Column)
    ):
        return False
    actual = frozenset(
        {
            (
                aliases.get(str(left.table or ""), str(left.table or "")),
                str(left.name or ""),
            ),
            (
                aliases.get(str(right.table or ""), str(right.table or "")),
                str(right.name or ""),
            ),
        }
    )
    for relationship in relationships:
        for left_field, right_field in relationship.field_pairs:
            expected = frozenset(
                {
                    _split_field(left_field),
                    _split_field(right_field),
                }
            )
            if actual == expected:
                return True
    return False


def _split_field(value: str) -> tuple[str, str]:
    model, separator, column = str(value).partition(".")
    return model, column if separator else ""


def filter_match(
    atoms: tuple[exp.Expression, ...],
    required: MdlFilterBinding,
    *,
    aliases: Mapping[str, str],
) -> tuple[tuple[int, ...], str]:
    statuses = tuple(
        filter_match_status(
            atom,
            required,
            aliases=aliases,
        )
        for atom in atoms
    )
    matches = tuple(
        index
        for index, status in enumerate(statuses)
        if status == "match"
    )
    if matches:
        return (matches[0],), "match"
    mismatches = tuple(
        index
        for index, status in enumerate(statuses)
        if status == "mismatch"
    )
    if mismatches:
        return (mismatches[0],), "mismatch"
    if str(required.op or "").lower() != "between":
        return (), "none"

    values = tuple(required.value or ())
    if len(values) != 2:
        return (), "none"
    lower = next(
        (
            index
            for index, atom in enumerate(atoms)
            if filter_part_matches(
                atom,
                required,
                op=">=",
                value=values[0],
                aliases=aliases,
            )
        ),
        None,
    )
    upper = next(
        (
            index
            for index, atom in enumerate(atoms)
            if filter_part_matches(
                atom,
                required,
                op="<",
                value=values[1],
                aliases=aliases,
            )
        ),
        None,
    )
    if lower is not None and upper is not None:
        return (lower, upper), "match"
    return (), "none"
