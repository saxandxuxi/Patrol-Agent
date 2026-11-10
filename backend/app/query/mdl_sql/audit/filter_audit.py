from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlglot import exp

from ..models import MdlFilterBinding, MdlQueryContract, SqlIssue
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
