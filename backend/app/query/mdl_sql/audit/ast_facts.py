from __future__ import annotations

from collections.abc import Iterable, Mapping

import sqlglot
from sqlglot import exp

from ..models import MdlQueryContract


def cte_names(expression: exp.Expression) -> set[str]:
    return {
        str(cte.alias_or_name)
        for cte in expression.find_all(exp.CTE)
        if str(cte.alias_or_name)
    }


def root_model(expression: exp.Expression, names: set[str]) -> str:
    select = root_select(expression)
    source = from_table(select)
    visited: set[str] = set()
    while source is not None:
        name = str(source.name or "")
        if name not in names or name in visited:
            return name
        visited.add(name)
        cte = next(
            (
                item
                for item in expression.find_all(exp.CTE)
                if str(item.alias_or_name) == name
            ),
            None,
        )
        if cte is None:
            return name
        source = from_table(root_select(cte.this))
    return ""


def root_select(expression: exp.Expression) -> exp.Select:
    current = expression
    while isinstance(current, exp.Subquery):
        current = current.this
    if isinstance(current, exp.Select):
        return current
    if isinstance(current, exp.SetOperation):
        return root_select(current.left)
    raise ValueError("parsed SQL has no select root")


def from_table(select: exp.Select) -> exp.Table | None:
    from_expression = (
        select.args.get("from_")
        or select.args.get("from")
    )
    value = getattr(from_expression, "this", None)
    if isinstance(value, exp.Table):
        return value
    if isinstance(value, exp.Subquery):
        return from_table(root_select(value.this))
    return None


def allowed_fields(
    contract: MdlQueryContract,
    dialect: str,
    relationship_fields: Iterable[tuple[str, str]] = (),
) -> set[tuple[str, str]]:
    fields = {
        (binding.model, binding.column)
        for binding in contract.field_bindings.values()
    }
    expressions: list[str] = [
        binding.expression
        for binding in (
            *contract.filters,
            *contract.mandatory_predicates,
        )
    ]
    expressions.extend(metric.expression for metric in contract.metrics)
    expressions.extend(output.expression for output in contract.outputs)
    expressions.extend(contract.group_by)
    expressions.extend(order.expression for order in contract.order_by)
    for value in expressions:
        parsed = parse_expression(value, dialect)
        for column in parsed.find_all(exp.Column):
            if column.table and column.name:
                fields.add((str(column.table), str(column.name)))
    fields.update(relationship_fields)
    return fields


def used_business_fields(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, str]] = []
    names = (
        rank_alias(expression)
        if "top_per_outer_group" in contract.required_assertions
        else ""
    )
    for select in expression.find_all(exp.Select):
        values: list[exp.Expression] = list(select.expressions)
        for name in ("where", "group", "having", "order"):
            clause = select.args.get(name)
            if clause is not None:
                values.append(clause)
        for value in values:
            for column in value.find_all(exp.Column):
                name = str(column.name or "")
                qualifier = str(column.table or "")
                if not qualifier and name in contract.output_aliases:
                    continue
                if not qualifier and names and name == names:
                    continue
                model = (
                    aliases.get(qualifier, qualifier)
                    if qualifier
                    else contract.root_model
                )
                if model and name:
                    fields.append((model, name))
    return tuple(dict.fromkeys(fields))


def clause_expressions(
    select: exp.Select,
    name: str,
) -> tuple[exp.Expression, ...]:
    clause = select.args.get(name)
    values = getattr(clause, "expressions", ()) if clause else ()
    return tuple(values)


def limit_value(select: exp.Select) -> int | None:
    limit = select.args.get("limit")
    value = getattr(limit, "expression", None) if limit else None
    if isinstance(value, exp.Literal):
        try:
            return int(value.this)
        except (TypeError, ValueError):
            return None
    return None


def projections(
    select: exp.Select,
) -> dict[str, exp.Expression]:
    result: dict[str, exp.Expression] = {}
    for item in select.expressions:
        if isinstance(item, exp.Alias):
            result[str(item.alias)] = item.this
        else:
            result[str(item.alias_or_name)] = item
    return result


def resolve_projection_alias(
    expression: exp.Expression,
    root_projections: Mapping[str, exp.Expression],
    alias: str,
) -> exp.Expression | None:
    value = root_projections.get(alias)
    if not (
        isinstance(value, exp.Column)
        and not value.table
        and value.name == alias
    ):
        return value
    for select in expression.find_all(exp.Select):
        candidate = projections(select).get(alias)
        if candidate is None:
            continue
        if (
            isinstance(candidate, exp.Column)
            and not candidate.table
            and candidate.name == alias
        ):
            continue
        return candidate
    return value


def contains_aggregate(expression: exp.Expression) -> bool:
    return any(
        isinstance(item, exp.AggFunc)
        for item in expression.walk()
    )


def parse_expression(value: str, dialect: str) -> exp.Expression:
    return sqlglot.parse_one(str(value or ""), read=dialect)


def canonical_expression(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    contract: MdlQueryContract,
    dialect: str,
) -> str:
    value = expression.this if isinstance(expression, exp.Alias) else expression
    copied = value.copy()

    def normalize(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            qualifier = str(node.table or "")
            model = (
                aliases.get(qualifier, qualifier)
                if qualifier
                else contract.root_model
            )
            return exp.column(str(node.name), table=model)
        return node

    return copied.transform(normalize).sql(dialect=dialect)


def rank_alias(expression: exp.Expression) -> str:
    for projection in expression.find_all(exp.Alias):
        value = projection.this
        if not isinstance(value, exp.Window):
            continue
        if isinstance(value.this, exp.RowNumber):
            return str(projection.alias)
    return ""
