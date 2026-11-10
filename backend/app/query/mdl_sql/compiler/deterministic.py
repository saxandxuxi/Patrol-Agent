from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp

from ..models import (
    CompilerFailure,
    MdlFilterBinding,
    MdlOrderBinding,
    MdlQueryContract,
    MdlSqlCandidate,
    PromptContextBundle,
)


_DIALECT = "mysql"
_RANK_ALIAS = "rank_in_outer"


def compile_contract_to_mdl_sql(
    contract: MdlQueryContract,
    context: PromptContextBundle,
) -> MdlSqlCandidate:
    try:
        _validate_context(contract, context)
        join_plan = _ordered_join_plan(contract, context)
        select = _build_select(contract, join_plan)
        mdl_sql = select.sql(dialect=_DIALECT)
        return MdlSqlCandidate(
            source="deterministic",
            mdl_sql=mdl_sql,
            contract_id=contract.contract_id,
            contract_hash=contract.contract_hash,
            used_models=tuple(
                [contract.root_model]
                + [joined_model for _, _, joined_model in join_plan]
            ),
            used_relationship_ids=tuple(
                relationship_id
                for relationship_id, _, _ in join_plan
            ),
        )
    except _NotCovered as exc:
        return _failure_candidate(
            contract,
            code="compiler_not_covered",
            reason=str(exc),
        )
    except Exception as exc:
        return _failure_candidate(
            contract,
            code="compiler_exception",
            reason="deterministic_sql_assembly_failed",
            exception_type=type(exc).__name__,
            exception_message=str(exc),
        )


def _validate_context(
    contract: MdlQueryContract,
    context: PromptContextBundle,
) -> None:
    if context.contract.contract_hash != contract.contract_hash:
        raise _NotCovered("context_contract_hash_mismatch")
    if context.contract.contract_id != contract.contract_id:
        raise _NotCovered("context_contract_id_mismatch")
    if contract.root_model not in context.mdl_subset.models:
        raise _NotCovered("root_model_missing_from_context")
    if not contract.outputs:
        raise _NotCovered("contract_has_no_outputs")
    if any(not output.alias for output in contract.outputs):
        raise _NotCovered("contract_output_alias_missing")
    if (
        contract.view == "top_groups"
        and "top_per_outer_group" in contract.required_assertions
        and len(contract.field_bindings) < 2
    ):
        raise _NotCovered("top_per_outer_group_dimensions_missing")


def _build_select(
    contract: MdlQueryContract,
    join_plan: tuple[tuple[str, Mapping[str, Any], str], ...],
) -> exp.Select:
    if (
        contract.view == "top_groups"
        and "top_per_outer_group" in contract.required_assertions
    ):
        return _build_ranked_top_groups(contract, join_plan)

    select = exp.Select(
        expressions=[
            _output_expression(output.expression, output.alias)
            for output in contract.outputs
        ]
    ).from_(_table(contract.root_model))
    _apply_joins(select, join_plan, contract)
    _apply_where(select, contract)

    if contract.group_by:
        select.set(
            "group",
            exp.Group(
                expressions=[
                    _parse_expression(value)
                    for value in contract.group_by
                ]
            ),
        )
    if contract.order_by:
        select.set(
            "order",
            exp.Order(
                expressions=[
                    _ordered_expression(order)
                    for order in contract.order_by
                ]
            ),
        )
    if contract.limit is not None:
        select.set(
            "limit",
            exp.Limit(
                expression=exp.Literal.number(contract.limit),
            ),
        )
    return select


def _build_ranked_top_groups(
    contract: MdlQueryContract,
    join_plan: tuple[tuple[str, Mapping[str, Any], str], ...],
) -> exp.Select:
    if not contract.dimensions:
        raise _NotCovered("top_per_outer_group_dimensions_missing")
    if not contract.order_by:
        raise _NotCovered("top_per_outer_group_order_missing")
    if contract.limit is None:
        raise _NotCovered("top_per_outer_group_limit_missing")

    outer_dimension = contract.field_bindings.get(
        contract.dimensions[0]
    )
    if outer_dimension is None:
        raise _NotCovered("top_per_outer_group_dimension_binding_missing")

    inner = exp.Select(
        expressions=[
            _output_expression(output.expression, output.alias)
            for output in contract.outputs
        ]
    )
    inner.expressions.append(
        exp.alias_(
            exp.Window(
                this=exp.RowNumber(),
                partition_by=[_parse_expression(outer_dimension.expression)],
                order=exp.Order(
                    expressions=[
                        _ordered_expression(order)
                        for order in contract.order_by
                    ]
                ),
            ),
            _RANK_ALIAS,
        )
    )
    inner = inner.from_(_table(contract.root_model))
    _apply_joins(inner, join_plan, contract)
    _apply_where(inner, contract)
    if contract.group_by:
        inner.set(
            "group",
            exp.Group(
                expressions=[
                    _parse_expression(value)
                    for value in contract.group_by
                ]
            ),
        )

    return (
        exp.Select(
            expressions=[
                _parse_expression(output.alias)
                for output in contract.outputs
            ]
        )
        .from_(
            exp.Subquery(
                this=inner,
                alias=exp.TableAlias(
                    this=exp.to_identifier("ranked"),
                ),
            )
        )
        .where(
            exp.LTE(
                this=exp.column(_RANK_ALIAS),
                expression=exp.Literal.number(contract.limit),
            )
        )
    )


def _ordered_join_plan(
    contract: MdlQueryContract,
    context: PromptContextBundle,
) -> tuple[tuple[str, Mapping[str, Any], str], ...]:
    relationships = {
        str(item.get("relationship_id") or ""): item
        for item in context.mdl_subset.relationships
    }
    unknown = sorted(
        set(contract.required_relationship_ids) - set(relationships)
    )
    if unknown:
        raise _NotCovered(
            "required_relationship_missing_from_context:"
            + ",".join(unknown)
        )

    pending = {
        relationship_id: relationships[relationship_id]
        for relationship_id in contract.required_relationship_ids
    }
    connected = {contract.root_model}
    plan: list[tuple[str, Mapping[str, Any], str]] = []
    while pending:
        candidates: list[tuple[str, Mapping[str, Any], str]] = []
        for relationship_id, relationship in pending.items():
            models = tuple(str(item) for item in relationship["models"])
            connected_models = [model for model in models if model in connected]
            disconnected_models = [
                model for model in models if model not in connected
            ]
            if len(connected_models) == 1 and len(disconnected_models) == 1:
                candidates.append(
                    (
                        relationship_id,
                        relationship,
                        disconnected_models[0],
                    )
                )
        if not candidates:
            raise _NotCovered("relationship_subgraph_is_disconnected")
        relationship_id, relationship, joined_model = sorted(
            candidates,
            key=lambda item: item[0],
        )[0]
        plan.append((relationship_id, relationship, joined_model))
        connected.add(joined_model)
        pending.pop(relationship_id)

    missing_models = sorted(
        set(contract.allowed_models) - connected
    )
    if missing_models:
        raise _NotCovered(
            "allowed_models_not_joined:" + ",".join(missing_models)
        )
    return tuple(plan)


def _apply_joins(
    select: exp.Select,
    join_plan: tuple[tuple[str, Mapping[str, Any], str], ...],
    contract: MdlQueryContract,
) -> None:
    for _, relationship, joined_model in join_plan:
        predicates = [_join_condition(relationship)]
        predicates.extend(
            expression
            for binding in contract.mandatory_predicates
            if binding.model == joined_model
            for expression in _filter_expressions(binding)
        )
        select.join(
            _table(joined_model),
            on=_conjoin(
                predicates,
                "join_condition_missing",
            ),
            join_type="left",
            copy=False,
        )


def _join_condition(relationship: Mapping[str, Any]) -> exp.Expression:
    pairs = relationship.get("field_pairs") or ()
    expressions: list[exp.Expression] = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise _NotCovered("relationship_field_pair_invalid")
        expressions.append(
            exp.EQ(
                this=_parse_expression(str(pair[0])),
                expression=_parse_expression(str(pair[1])),
            )
        )
    return _conjoin(expressions, "relationship_condition_missing")


def _apply_where(
    select: exp.Select,
    contract: MdlQueryContract,
) -> None:
    predicates: list[exp.Expression] = []
    root_predicates = tuple(
        binding
        for binding in contract.mandatory_predicates
        if binding.model == contract.root_model
    )
    for binding in (*contract.filters, *root_predicates):
        predicates.extend(_filter_expressions(binding))
    if predicates:
        select.set(
            "where",
            exp.Where(this=_conjoin(predicates, "where_empty")),
        )


def _filter_expressions(
    binding: MdlFilterBinding,
) -> tuple[exp.Expression, ...]:
    column = _parse_expression(binding.expression)
    if not isinstance(column, exp.Column):
        raise _NotCovered(
            f"filter_expression_is_not_column:{binding.field_id}"
        )
    op = str(binding.op or "").lower()
    value = binding.value

    if op == "between":
        values = _sequence(value, expected=2)
        if binding.between_inclusive:
            return (
                exp.Between(
                    this=column,
                    low=exp.convert(values[0]),
                    high=exp.convert(values[1]),
                ),
            )
        return (
            exp.GTE(
                this=column.copy(),
                expression=exp.convert(values[0]),
            ),
            exp.LT(
                this=column.copy(),
                expression=exp.convert(values[1]),
            ),
        )
    if op in {"=", "empty"} and isinstance(
        value,
        (list, tuple, set, frozenset),
    ):
        return (
            exp.In(
                this=column,
                expressions=[exp.convert(item) for item in value],
            ),
        )
    if op in {"in", "not_in"}:
        values = _sequence(value)
        expression: exp.Expression = exp.In(
            this=column,
            expressions=[exp.convert(item) for item in values],
        )
        if op == "not_in":
            expression = exp.Not(this=expression)
        return (expression,)
    if op in {"is_null"}:
        return (exp.Is(this=column, expression=exp.Null()),)
    if op in {"not_null"}:
        return (
            exp.Not(
                this=exp.Is(this=column, expression=exp.Null()),
            ),
        )

    normalized = {
        "empty": "=",
        "not_empty": "!=",
    }.get(op, op)
    operator = {
        "=": exp.EQ,
        "!=": exp.NEQ,
        ">": exp.GT,
        ">=": exp.GTE,
        "<": exp.LT,
        "<=": exp.LTE,
    }.get(normalized)
    if operator is None:
        raise _NotCovered(f"filter_operator_not_covered:{op}")
    resolved_value = "" if op == "empty" and value is None else value
    return (
        operator(
            this=column,
            expression=exp.convert(resolved_value),
        ),
    )


def _output_expression(
    value: str,
    alias: str,
) -> exp.Alias:
    return exp.alias_(
        _parse_expression(value),
        alias,
        quoted=False,
    )


def _ordered_expression(binding: MdlOrderBinding) -> exp.Ordered:
    descending = binding.direction == "desc"
    return exp.Ordered(
        this=_parse_expression(binding.expression),
        desc=descending,
        nulls_first=not descending,
    )


def _parse_expression(value: str) -> exp.Expression:
    text = str(value or "").strip()
    if not text:
        raise _NotCovered("empty_sql_expression")
    expression = sqlglot.parse_one(text, read=_DIALECT)
    if expression is None:
        raise _NotCovered("sql_expression_parse_failed")
    return expression


def _table(model: str) -> exp.Table:
    return exp.Table(this=exp.to_identifier(model))


def _conjoin(
    expressions: Sequence[exp.Expression],
    missing_reason: str,
) -> exp.Expression:
    if not expressions:
        raise _NotCovered(missing_reason)
    result = expressions[0]
    for expression in expressions[1:]:
        result = exp.and_(result, expression, copy=False)
    return result


def _sequence(
    value: Any,
    *,
    expected: int | None = None,
) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise _NotCovered("filter_value_is_not_a_sequence")
    resolved = tuple(value)
    if not resolved:
        raise _NotCovered("filter_value_sequence_is_empty")
    if expected is not None and len(resolved) != expected:
        raise _NotCovered("filter_value_sequence_has_wrong_length")
    return resolved


def _failure_candidate(
    contract: MdlQueryContract,
    *,
    code: str,
    reason: str,
    **evidence: Any,
) -> MdlSqlCandidate:
    return MdlSqlCandidate(
        source="deterministic",
        mdl_sql="",
        contract_id=contract.contract_id,
        contract_hash=contract.contract_hash,
        failure=CompilerFailure(
            code=code,
            stage="deterministic",
            contract_id=contract.contract_id,
            contract_hash=contract.contract_hash,
            evidence={
                "reason": reason,
                "contract_id": contract.contract_id,
                "contract_hash": contract.contract_hash,
                "subject": contract.subject,
                "root_model": contract.root_model,
                "view": contract.view,
                **evidence,
            },
        ),
    )


class _NotCovered(Exception):
    pass
