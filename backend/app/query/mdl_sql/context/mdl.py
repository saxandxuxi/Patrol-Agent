from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import sqlglot
from sqlglot import exp

from ..errors import MdlSqlConfigurationError
from ..models import MdlContext, MdlQueryContract, MdlSnapshot
from .relationships import relationship_context


def build_mdl_context(
    contract: MdlQueryContract,
    snapshot: MdlSnapshot,
) -> MdlContext:
    allowed_models = tuple(contract.allowed_models)
    unknown_models = sorted(set(allowed_models) - set(snapshot.models))
    if unknown_models:
        _raise_configuration(
            contract,
            "unknown_mdl_model",
            models=unknown_models,
        )

    columns: dict[str, set[str]] = defaultdict(set)
    for binding in contract.field_bindings.values():
        columns[binding.model].add(binding.column)
    for binding in (*contract.filters, *contract.mandatory_predicates):
        columns[binding.model].add(binding.column)
    for metric in contract.metrics:
        _add_expression_columns(
            columns,
            metric.expression,
            allowed_models=set(allowed_models),
            root_model=contract.root_model,
            contract=contract,
        )
    for order in contract.order_by:
        _add_expression_columns(
            columns,
            order.expression,
            allowed_models=set(allowed_models),
            root_model=contract.root_model,
            contract=contract,
        )

    for relationship_id in contract.required_relationship_ids:
        relationship = snapshot.relationships.get(relationship_id)
        if relationship is None:
            _raise_configuration(
                contract,
                "unknown_relationship",
                relationship_id=relationship_id,
            )
        for pair in relationship.field_pairs:
            for reference in pair:
                model, column = reference.split(".", 1)
                if model in allowed_models:
                    columns[model].add(column)
    relationships = relationship_context(
        snapshot,
        contract.required_relationship_ids,
    )

    models: dict[str, Mapping[str, Any]] = {}
    for model_name in allowed_models:
        model = snapshot.models[model_name]
        for column_name, column in model.columns.items():
            if column.is_primary_key:
                columns[model_name].add(column_name)
        selected_columns: dict[str, Mapping[str, Any]] = {}
        for column_name in sorted(columns[model_name]):
            column = model.columns.get(column_name)
            if column is None:
                _raise_configuration(
                    contract,
                    "unknown_mdl_column",
                    model=model_name,
                    column=column_name,
                )
            selected_columns[column_name] = {
                "data_type": column.data_type,
                "description": column.description,
                "is_calculated": column.is_calculated,
                "expression": column.expression,
                "is_primary_key": column.is_primary_key,
            }
        models[model_name] = MappingProxyType(
            {
                "description": model.description,
                "columns": MappingProxyType(selected_columns),
            }
        )

    return MdlContext(
        root_model=contract.root_model,
        models=MappingProxyType(models),
        relationships=tuple(relationships),
    )


def _add_expression_columns(
    columns: dict[str, set[str]],
    expression: str,
    *,
    allowed_models: set[str],
    root_model: str,
    contract: MdlQueryContract,
) -> None:
    try:
        parsed = sqlglot.parse_one(
            f"SELECT {expression}",
            read="mysql",
        )
    except sqlglot.errors.ParseError as exc:
        raise MdlSqlConfigurationError(
            "invalid_mdl_catalog",
            {
                "reason": "context_expression_parse_failed",
                "contract_id": contract.contract_id,
                "contract_hash": contract.contract_hash,
                "expression": expression,
            },
        ) from exc
    for column in parsed.find_all(exp.Column):
        model = str(column.table or root_model)
        if model not in allowed_models:
            _raise_configuration(
                contract,
                "expression_model_not_allowed",
                model=model,
                expression=expression,
            )
        columns[model].add(column.name)


def _raise_configuration(
    contract: MdlQueryContract,
    reason: str,
    **evidence: Any,
) -> None:
    raise MdlSqlConfigurationError(
        "invalid_mdl_catalog",
        {
            "reason": reason,
            "contract_id": contract.contract_id,
            "contract_hash": contract.contract_hash,
            **evidence,
        },
    )
