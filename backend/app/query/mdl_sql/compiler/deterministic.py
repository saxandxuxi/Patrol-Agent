from __future__ import annotations

from collections import deque
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
        filter_paths = _filter_only_relationship_paths(
            contract,
            context,
        )
        main_models = _main_reference_models(contract, context)
        filter_path_ids = {
            str(relationship.get("relationship_id") or "")
            for path in filter_paths.values()
            for relationship in path
        }
        main_path_ids = {
            str(relationship.get("relationship_id") or "")
            for model in main_models - {contract.root_model}
            for relationship in _relationship_path(
                contract.root_model,
                model,
                context,
            )
        }
        relationship_ids = tuple(
            relationship_id
            for relationship_id in contract.required_relationship_ids
            if relationship_id not in filter_path_ids - main_path_ids
        )
        join_plan = _ordered_join_plan(
            contract,
            context,
            relationship_ids=relationship_ids,
            required_models=frozenset(main_models),
        )
        select = _build_select(
            contract,
            join_plan,
            filter_paths,
        )
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
                dict.fromkeys(
                    (
                        *sorted(filter_path_ids),
                        *(
                            relationship_id
                            for relationship_id, _, _ in join_plan
                        ),
                    )
                )
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
    filter_paths: Mapping[
        str,
        tuple[Mapping[str, Any], ...],
    ],
) -> exp.Select:
    if (
        contract.view == "top_groups"
        and "top_per_outer_group" in contract.required_assertions
    ):
        return _build_ranked_top_groups(
            contract,
            join_plan,
            filter_paths,
        )

    select = exp.Select(
        expressions=[
            _output_expression(output.expression, output.alias)
            for output in contract.outputs
        ]
    ).from_(_table(contract.root_model))
    _apply_joins(select, join_plan, contract)
    _apply_where(select, contract, filter_paths)

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
    filter_paths: Mapping[
        str,
        tuple[Mapping[str, Any], ...],
    ],
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
    _apply_where(inner, contract, filter_paths)
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


def _main_reference_models(
    contract: MdlQueryContract,
    context: PromptContextBundle,
) -> set[str]:
    known_models = set(context.mdl_subset.models)
    models = {contract.root_model}
    for binding in contract.field_bindings.values():
        if set(binding.contexts) - {"filters", "mandatory_predicates"}:
            if binding.model in known_models:
                models.add(binding.model)
    expressions = [
        output.expression for output in contract.outputs
    ]
    expressions.extend(metric.expression for metric in contract.metrics)
    expressions.extend(contract.group_by)
    expressions.extend(order.expression for order in contract.order_by)
    for value in expressions:
        for column in _parse_expression(value).find_all(exp.Column):
            model = str(column.table or "")
            if model in known_models:
                models.add(model)
    return models


def _filter_only_relationship_paths(
    contract: MdlQueryContract,
    context: PromptContextBundle,
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    main_models = _main_reference_models(contract, context)
    paths: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for binding in contract.filters:
        model = str(binding.model or "")
        if model in main_models or model == contract.root_model:
            continue
        paths[model] = _relationship_path(
            contract.root_model,
            model,
            context,
        )
    return paths


def _relationship_path(
    root_model: str,
    target_model: str,
    context: PromptContextBundle,
) -> tuple[Mapping[str, Any], ...]:
    if root_model == target_model:
        return ()
    adjacency: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for relationship in context.mdl_subset.relationships:
        models = tuple(str(model) for model in relationship.get("models") or ())
        if len(models) != 2:
            continue
        left, right = models
        adjacency.setdefault(left, []).append((right, relationship))
        adjacency.setdefault(right, []).append((left, relationship))
    for edges in adjacency.values():
        edges.sort(
            key=lambda item: str(
                item[1].get("relationship_id") or ""
            )
        )

    queue = deque([(root_model, ())])
    visited = {root_model}
    while queue:
        current, path = queue.popleft()
        for neighbor, relationship in adjacency.get(current, ()):
            next_path = (*path, relationship)
            if neighbor == target_model:
                return next_path
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, next_path))
    raise _NotCovered(
        f"filter_relationship_path_missing:{root_model}->{target_model}"
    )


def _ordered_join_plan(
    contract: MdlQueryContract,
    context: PromptContextBundle,
    *,
    relationship_ids: tuple[str, ...] | None = None,
    required_models: frozenset[str] | None = None,
) -> tuple[tuple[str, Mapping[str, Any], str], ...]:
    relationships = {
        str(item.get("relationship_id") or ""): item
        for item in context.mdl_subset.relationships
    }
    selected_ids = (
        contract.required_relationship_ids
        if relationship_ids is None
        else relationship_ids
    )
    unknown = sorted(set(selected_ids) - set(relationships))
    if unknown:
        raise _NotCovered(
            "required_relationship_missing_from_context:"
            + ",".join(unknown)
        )

    pending = {
        relationship_id: relationships[relationship_id]
        for relationship_id in selected_ids
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

    expected_models = (
        frozenset(contract.allowed_models)
        if required_models is None
        else required_models
    )
    missing_models = sorted(set(expected_models) - connected)
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
    filter_paths: Mapping[
        str,
        tuple[Mapping[str, Any], ...],
    ],
) -> None:
    predicates: list[exp.Expression] = []
    root_predicates = tuple(
        binding
        for binding in contract.mandatory_predicates
        if binding.model == contract.root_model
    )
    root_filters = tuple(
        binding
        for binding in contract.filters
        if binding.model == contract.root_model
    )
    for binding in (*root_filters, *root_predicates):
        predicates.extend(_filter_expressions(binding))
    for binding in contract.filters:
        if (
            binding.model == contract.root_model
            or binding.model in filter_paths
        ):
            continue
        predicates.extend(_filter_expressions(binding))
    for model, path in filter_paths.items():
        filters = tuple(
            binding
            for binding in contract.filters
            if binding.model == model
        )
        predicates.append(
            _exists_filter(
                contract,
                path,
                filters,
            )
        )
    if predicates:
        select.set(
            "where",
            exp.Where(this=_conjoin(predicates, "where_empty")),
        )


def _exists_filter(
    contract: MdlQueryContract,
    path: tuple[Mapping[str, Any], ...],
    filters: tuple[MdlFilterBinding, ...],
) -> exp.Expression:
    if not path:
        raise _NotCovered("filter_relationship_path_empty")
    connected = {contract.root_model}
    joined_models: list[str] = []
    for relationship in path:
        models = tuple(
            str(model) for model in relationship.get("models") or ()
        )
        next_model = next(
            (model for model in models if model not in connected),
            "",
        )
        if not next_model:
            raise _NotCovered("filter_relationship_path_disconnected")
        joined_models.append(next_model)
        connected.add(next_model)

    inner = exp.Select(
        expressions=[exp.Literal.number(1)]
    ).from_(_table(joined_models[0]))
    for relationship, joined_model in zip(
        path[1:],
        joined_models[1:],
    ):
        inner = inner.join(
            _table(joined_model),
            on=_join_condition(relationship),
            join_type="inner",
            copy=False,
        )
    predicates = [_join_condition(path[0])]
    predicates.extend(
        expression
        for binding in contract.mandatory_predicates
        if binding.model in joined_models
        for expression in _filter_expressions(binding)
    )
    for binding in filters:
        predicates.extend(_filter_expressions(binding))
    inner = inner.where(
        _conjoin(predicates, "filter_exists_empty")
    )
    return exp.Exists(
        this=exp.Subquery(this=inner)
    )


def _filter_expressions(
    binding: MdlFilterBinding,
) -> tuple[exp.Expression, ...]:
    op = str(binding.op or "").lower()
    if op == "exists":
        return (_parse_expression(binding.expression),)
    column = _parse_expression(binding.expression)
    if not isinstance(column, exp.Column):
        raise _NotCovered(
            f"filter_expression_is_not_column:{binding.field_id}"
        )
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
