from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import sqlglot
from sqlglot import exp

from ...semantic_aids import pile_meter
from ..capability_registry import CapabilityRegistry
from ..contracts import QuerySpec, scope_hash
from ..shape_contracts import (
    canonical_view,
    normalize_order_items,
    normalize_top_view,
    validate_shape_contract,
)
from .context import build_relationship_subgraph
from .errors import MdlSqlContractError
from .models import (
    MdlFieldBinding,
    MdlFilterBinding,
    MdlMetricBinding,
    MdlOrderBinding,
    MdlOutputBinding,
    MdlQueryContract,
    MdlSnapshot,
)


_GROUPED_VIEWS = frozenset({"distribution", "trend", "top_groups"})
_METRIC_ONLY_VIEWS = frozenset({"count", "sum", "avg"})
_DISTINCT_RECORD_VIEWS = frozenset({"data", "detail", "list"})
_DIMENSION_ROLES = frozenset(
    {"dimension", "code", "primary", "identity", "fk"}
)


def build_mdl_query_contract(
    spec: QuerySpec,
    registry: CapabilityRegistry,
    snapshot: MdlSnapshot,
    *,
    plan_id: str = "",
) -> MdlQueryContract:
    normalized = _normalize_and_validate_spec(spec, registry)
    subject = normalized.subject
    capability = registry.snapshot.subjects[subject]
    root_model = capability.mdl_model
    if root_model not in snapshot.models:
        _raise_contract(
            normalized,
            "root_model_missing",
            root_model=root_model,
        )

    field_refs: dict[str, dict[str, Any]] = {}
    required_join_ids: list[str] = []
    required_join_models: set[str] = set()

    def add_requires_join(
        values: tuple[str, ...],
        *,
        path: str,
    ) -> None:
        for value in values:
            reference = str(value or "")
            if reference in snapshot.relationships:
                required_join_ids.append(reference)
            elif reference in snapshot.models:
                direct_relationship_ids = sorted(
                    relationship.relationship_id
                    for relationship in snapshot.relationships.values()
                    if set(relationship.models) == {root_model, reference}
                )
                if len(direct_relationship_ids) == 1:
                    required_join_ids.append(direct_relationship_ids[0])
                elif direct_relationship_ids:
                    _raise_contract(
                        normalized,
                        "ambiguous_requires_join",
                        path=path,
                        reference=reference,
                        candidate_relationship_ids=direct_relationship_ids,
                    )
                else:
                    required_join_models.add(reference)
            else:
                _raise_contract(
                    normalized,
                    "unknown_requires_join",
                    path=path,
                    reference=reference,
                )

    def add_field_reference(
        field_id: str,
        context: str,
        *,
        path: str,
    ) -> MdlFieldBinding:
        base_field = _base_id(field_id)
        field_capability = registry.snapshot.fields.get(
            subject,
            {},
        ).get(base_field)
        if (
            field_capability is None
            or field_capability.status != "available"
        ):
            reason = (
                "unavailable_field"
                if field_capability is not None
                else "unknown_field"
            )
            _raise_contract(
                normalized,
                reason,
                path=path,
                field=base_field,
                status=(
                    field_capability.status
                    if field_capability is not None
                    else "missing"
                ),
                unavailable_reason=(
                    field_capability.unavailable_reason
                    if field_capability is not None
                    else ""
                ),
            )
        if (
            context == "dimensions"
            and not _DIMENSION_ROLES.intersection(field_capability.roles)
        ):
            _raise_contract(
                normalized,
                "field_role_not_allowed",
                path=path,
                field=base_field,
                context=context,
                allowed_roles=list(field_capability.roles),
            )
        if (
            context != "filters"
            and "relation_exists" in field_capability.roles
        ):
            _raise_contract(
                normalized,
                "relation_exists_filter_only",
                path=path,
                field=base_field,
                context=context,
            )

        model = _capability_model(
            normalized,
            snapshot,
            field_capability.physical_table,
            path=path,
            field=base_field,
        )
        column = str(field_capability.physical_column or base_field)
        if column not in snapshot.models[model].columns:
            _raise_contract(
                normalized,
                "field_not_in_mdl",
                path=path,
                field=base_field,
                model=model,
                column=column,
            )
        expression = _normalize_expression(
            normalized,
            snapshot,
            str(field_capability.expression or column),
            root_model=root_model,
            target_model=model,
            path=path,
            field=base_field,
        )
        add_requires_join(
            field_capability.requires_join,
            path=f"{path}.requires_join",
        )

        binding = MdlFieldBinding(
            field_id=base_field,
            model=model,
            column=column,
            expression=expression,
            label=field_capability.label,
            data_type=field_capability.data_type,
            unit=field_capability.unit,
            source="query_spec",
            roles=field_capability.roles,
            contexts=(context,),
        )
        existing = field_refs.get(base_field)
        if existing is None:
            field_refs[base_field] = {
                "binding": binding,
                "contexts": {context},
            }
        else:
            field_refs[base_field]["contexts"].add(context)
        return binding

    filters: list[MdlFilterBinding] = []
    for index, value in enumerate(normalized.filters):
        filter_field_id = _filter_storage_field_id(
            registry,
            value.field,
            value.resolver,
        )
        field_binding = add_field_reference(
            filter_field_id,
            "filters",
            path=f"filters[{index}].field",
        )
        if not value.source:
            _raise_contract(
                normalized,
                "missing_filter_source",
                path=f"filters[{index}].source",
                field=field_binding.field_id,
            )
        field_capability = registry.snapshot.fields[subject][
            field_binding.field_id
        ]
        relation_exists = (
            "relation_exists" in field_capability.roles
        )
        filter_op = str(value.op or "").lower()
        if relation_exists and filter_op != "exists":
            _raise_contract(
                normalized,
                "relation_exists_requires_exists",
                path=f"filters[{index}].op",
                field=field_binding.field_id,
                op=filter_op,
            )
        if not relation_exists and filter_op == "exists":
            _raise_contract(
                normalized,
                "relation_predicate_not_registered",
                path=f"filters[{index}].op",
                field=field_binding.field_id,
            )
        expression = _normalize_expression(
            normalized,
            snapshot,
            str(
                field_capability.filter_expression
                or field_capability.expression
            ),
            root_model=root_model,
            target_model=field_binding.model,
            path=f"filters[{index}]",
            field=field_binding.field_id,
        )
        filter_value, between_inclusive = _normalize_filter_value(
            normalized,
            registry,
            field_capability,
            str(value.op),
            value.storage_value,
            path=f"filters[{index}].storage_value",
        )
        filter_model, filter_column = _filter_location(
            expression,
            default_model=field_binding.model,
            default_column=field_binding.column,
            dialect=str(normalized.kind or "mysql"),
        )
        filters.append(
            MdlFilterBinding(
                field_id=field_binding.field_id,
                op=str(value.op),
                value=_freeze_value(filter_value),
                display_value=value.display_value,
                source=value.source,
                resolver=value.resolver,
                raw_text=value.raw_text,
                model=filter_model,
                column=filter_column,
                expression=expression,
                between_inclusive=between_inclusive,
            )
        )

    dimensions = tuple(
        dict.fromkeys(_base_id(item) for item in normalized.dimensions)
    )
    for index, field_id in enumerate(dimensions):
        add_field_reference(
            field_id,
            "dimensions",
            path=f"dimensions[{index}]",
        )

    metric_ids = tuple(
        dict.fromkeys(_base_id(item) for item in normalized.metrics)
    )
    metric_bindings: list[MdlMetricBinding] = []
    metric_models: set[str] = set()
    for index, metric_id in enumerate(metric_ids):
        metric = registry.snapshot.metrics.get(subject, {}).get(metric_id)
        if metric is None:
            _raise_contract(
                normalized,
                "unknown_metric",
                path=f"metrics[{index}]",
                metric=metric_id,
                available_metrics=sorted(
                    registry.snapshot.metrics.get(subject, {})
                ),
            )
        if (
            metric.allowed_views
            and normalized.view not in metric.allowed_views
        ):
            _raise_contract(
                normalized,
                "metric_view_not_allowed",
                path=f"metrics[{index}]",
                metric=metric_id,
                view=normalized.view,
                allowed_views=list(metric.allowed_views),
            )
        expression, referenced_models = _normalize_metric_expression(
            normalized,
            snapshot,
            metric.expression,
            root_model=root_model,
            path=f"metrics[{index}]",
        )
        add_requires_join(
            metric.requires_join,
            path=f"metrics[{index}].requires_join",
        )
        metric_models.update(referenced_models)
        metric_bindings.append(
            MdlMetricBinding(
                metric_id=metric_id,
                expression=expression,
                label=metric.label,
                unit=metric.unit,
                source="query_spec",
                requires_join=metric.requires_join,
                aggregation=metric.aggregation,
                grain=metric.grain,
                identity=metric.identity,
                fanout_safe=metric.fanout_safe,
            )
        )
    metrics_by_id = {
        metric.metric_id: metric
        for metric in metric_bindings
    }

    output_ids = _effective_output_ids(normalized, dimensions, metric_ids)
    for index, output_id in enumerate(output_ids):
        if output_id not in metrics_by_id:
            add_field_reference(
                output_id,
                "outputs",
                path=f"output_projection[{index}]",
            )

    outputs: list[MdlOutputBinding] = []
    output_aliases: dict[str, str] = {}
    for output_id in output_ids:
        metric = metrics_by_id.get(output_id)
        if metric is not None:
            alias = metric.label or output_id
            if alias in output_aliases:
                _raise_contract(
                    normalized,
                    "duplicate_output",
                    output=output_id,
                    alias=alias,
                )
            output_aliases[alias] = output_id
            outputs.append(
                MdlOutputBinding(
                    output_id=output_id,
                    kind="metric",
                    expression=metric.expression,
                    alias=alias,
                    label=metric.label,
                    unit=metric.unit,
                    source="query_spec",
                )
            )
            continue
        field_binding = field_refs[output_id]["binding"]
        alias = field_binding.label or output_id
        if alias in output_aliases:
            _raise_contract(
                normalized,
                "duplicate_output",
                output=output_id,
                alias=alias,
            )
        output_aliases[alias] = output_id
        outputs.append(
            MdlOutputBinding(
                output_id=output_id,
                kind="field",
                expression=field_binding.expression,
                alias=alias,
                label=field_binding.label,
                unit=field_binding.unit,
                source="query_spec",
            )
        )

    order_bindings: list[MdlOrderBinding] = []
    for index, raw_order in enumerate(normalized.order_by):
        if isinstance(raw_order, Mapping):
            field_id = _base_id(str(raw_order.get("field") or ""))
            direction = str(raw_order.get("direction") or "asc").lower()
        else:
            field_id = _base_id(str(raw_order or ""))
            direction = "asc"
        if not field_id:
            _raise_contract(
                normalized,
                "invalid_order",
                path=f"order_by[{index}]",
            )
        if direction not in {"asc", "desc"}:
            _raise_contract(
                normalized,
                "invalid_order_direction",
                path=f"order_by[{index}].direction",
                direction=direction,
            )
        metric = metrics_by_id.get(field_id)
        if metric is not None:
            expression = metric.expression
        else:
            if field_id not in field_refs:
                add_field_reference(
                    field_id,
                    "order_by",
                    path=f"order_by[{index}].field",
                )
            expression = field_refs[field_id]["binding"].expression
        order_bindings.append(
            MdlOrderBinding(
                field_id=field_id,
                expression=expression,
                direction=direction,
                source="query_spec.order_by",
            )
        )

    active_models = {
        root_model,
        *metric_models,
        *required_join_models,
        *(
            record["binding"].model
            for record in field_refs.values()
        ),
    }
    mandatory_predicates: list[MdlFilterBinding] = []
    for field_id, field_capability in registry.snapshot.fields.get(
        subject,
        {},
    ).items():
        if "soft_delete" not in field_capability.roles:
            continue
        mandatory_model = (
            root_model
            if field_capability.physical_table
            == capability.base_table
            else snapshot.physical_table_to_model.get(
                field_capability.physical_table,
                "",
            )
        )
        if mandatory_model not in active_models:
            continue
        field_binding = add_field_reference(
            field_id,
            "mandatory_predicates",
            path="capability.fields.soft_delete",
        )
        expression = _normalize_expression(
            normalized,
            snapshot,
            str(
                field_capability.filter_expression
                or field_capability.expression
            ),
            root_model=root_model,
            target_model=field_binding.model,
            path="capability.fields.soft_delete",
            field=field_id,
        )
        mandatory_predicates.append(
            MdlFilterBinding(
                field_id=field_id,
                op="=",
                value=0,
                display_value="0",
                source="capability_registry",
                resolver="field_role:soft_delete",
                raw_text="",
                model=field_binding.model,
                column=field_binding.column,
                expression=expression,
            )
        )

    field_bindings = MappingProxyType(
        {
            field_id: replace(
                record["binding"],
                contexts=tuple(sorted(record["contexts"])),
            )
            for field_id, record in field_refs.items()
        }
    )
    has_identity_dimension = any(
        "identity" in field_bindings[field_id].roles
        for field_id in dimensions
    )
    effective_outputs: list[MdlOutputBinding] = []
    for output in outputs:
        binding = field_bindings.get(output.output_id)
        if (
            output.kind == "field"
            and binding is not None
            and has_identity_dimension
            and "multi_value" in binding.roles
        ):
            output = replace(
                output,
                expression=(
                    "GROUP_CONCAT("
                    f"DISTINCT {binding.expression} "
                    f"ORDER BY {binding.expression} SEPARATOR '、'"
                    ")"
                ),
            )
        effective_outputs.append(output)
    group_dimensions = (
        normalized.view in _GROUPED_VIEWS
        or (
            normalized.view in _DISTINCT_RECORD_VIEWS
            and dimensions
            and not metric_bindings
        )
    )
    group_by = tuple(
        field_bindings[field_id].expression
        for field_id in dimensions
        if not (
            has_identity_dimension
            and "multi_value" in field_bindings[field_id].roles
        )
    ) if group_dimensions else ()

    required_models = {
        field.model for field in field_bindings.values()
    }
    required_models.update(metric_models)
    required_models.update(required_join_models)
    required_models.add(root_model)
    subgraph = build_relationship_subgraph(
        snapshot,
        root_model=root_model,
        required_models=tuple(sorted(required_models)),
        required_relationship_ids=tuple(dict.fromkeys(required_join_ids)),
        max_hops=registry.snapshot.mdl_compilation.max_relationship_hops,
    )

    contract = MdlQueryContract(
        contract_id="",
        plan_id=str(plan_id or ""),
        node_id=normalized.id,
        query_spec_hash=scope_hash(normalized),
        capability_snapshot_hash=registry.snapshot.config_hash,
        mdl_fingerprint=snapshot.fingerprint,
        contract_hash="",
        subject=subject,
        view=normalized.view,
        root_model=root_model,
        field_bindings=field_bindings,
        filters=tuple(filters),
        dimensions=dimensions,
        metrics=tuple(metric_bindings),
        outputs=tuple(effective_outputs),
        group_by=group_by,
        order_by=tuple(order_bindings),
        limit=normalized.limit,
        required_assertions=normalized.required_assertions,
        allowed_models=subgraph.models,
        required_relationship_ids=tuple(
            relationship.relationship_id
            for relationship in subgraph.relationships
        ),
        intermediate_models=tuple(
            sorted(
                set(subgraph.models)
                - required_models
                - {root_model}
            )
        ),
        mandatory_predicates=tuple(mandatory_predicates),
        output_aliases=MappingProxyType(output_aliases),
        question=spec.question,
    )
    contract_hash = _contract_digest(contract)
    return replace(
        contract,
        contract_hash=contract_hash,
        contract_id=(
            f"{contract.plan_id or 'single'}:"
            f"{contract.node_id}:{contract_hash[:16]}"
        ),
    )


def _normalize_and_validate_spec(
    spec: QuerySpec,
    registry: CapabilityRegistry,
) -> QuerySpec:
    normalized = normalize_top_view(spec)
    normalized = replace(
        normalized,
        order_by=normalize_order_items(normalized.order_by),
    )
    normalized = replace(
        normalized,
        view=canonical_view(
            normalized.view,
            normalized.metrics,
            normalized.dimensions,
        ),
    )
    normalized = _normalize_trend_dimensions(normalized, registry)
    subject = registry.snapshot.subjects.get(normalized.subject)
    if subject is None:
        _raise_contract(
            normalized,
            "unknown_subject",
            subject=normalized.subject,
        )
    normalized = _canonicalize_metric_references(
        normalized,
        registry,
    )
    if normalized.view not in subject.allowed_views:
        _raise_contract(
            normalized,
            "unsupported_view",
            subject=normalized.subject,
            view=normalized.view,
            allowed_views=list(subject.allowed_views),
        )
    issues = validate_shape_contract(normalized, registry)
    if issues:
        issue = issues[0]
        _raise_contract(
            normalized,
            "invalid_view_shape",
            issue_code=issue.code,
            issue_reason=issue.reason,
        )
    if normalized.limit is not None:
        if (
            isinstance(normalized.limit, bool)
            or not isinstance(normalized.limit, int)
            or normalized.limit <= 0
        ):
            _raise_contract(
                normalized,
                "invalid_limit",
                limit=normalized.limit,
            )
    return normalized


def _canonicalize_metric_references(
    spec: QuerySpec,
    registry: CapabilityRegistry,
) -> QuerySpec:
    def metric_id(value: str) -> str:
        base = _base_id(value)
        metric = registry.metric(spec.subject, base)
        return metric.metric_id if metric is not None else base

    metrics = tuple(metric_id(item) for item in spec.metrics)
    output_projection = tuple(
        metric_id(item) for item in spec.output_projection
    )
    order_by = tuple(
        (
            {
                **item,
                "field": metric_id(str(item.get("field") or "")),
            }
            if isinstance(item, Mapping)
            else metric_id(str(item))
        )
        for item in spec.order_by
    )
    return replace(
        spec,
        metrics=metrics,
        output_projection=output_projection,
        order_by=order_by,
    )


def _normalize_trend_dimensions(
    spec: QuerySpec,
    registry: CapabilityRegistry,
) -> QuerySpec:
    if spec.view != "trend":
        return spec
    semantics = registry.snapshot.query_shapes.get(
        "planner_semantics"
    ) or {}
    answer_operations = semantics.get("answer_operations") or {}
    trend = (
        answer_operations.get("trend")
        or semantics.get("trend")
        or {}
    )
    grain = str(trend.get("default_time_dimension") or "")
    if not grain:
        return spec
    fields = registry.snapshot.fields.get(spec.subject, {})
    replacements: dict[str, str] = {}
    for field_id in spec.dimensions:
        base_field = _base_id(field_id)
        capability = fields.get(base_field)
        if capability is None or "time" not in capability.roles:
            continue
        candidate_id = f"{base_field}_{grain}"
        candidate = fields.get(candidate_id)
        if candidate is None or candidate.status != "available":
            continue
        replacements[base_field] = candidate_id
    if not replacements:
        return spec
    return replace(
        spec,
        dimensions=tuple(
            replacements.get(_base_id(field_id), _base_id(field_id))
            for field_id in spec.dimensions
        ),
        output_projection=tuple(
            replacements.get(_base_id(field_id), _base_id(field_id))
            for field_id in spec.output_projection
        ),
    )


def _capability_model(
    spec: QuerySpec,
    snapshot: MdlSnapshot,
    physical_table: str,
    *,
    path: str,
    field: str,
) -> str:
    model = snapshot.physical_table_to_model.get(str(physical_table or ""))
    if not model:
        _raise_contract(
            spec,
            "physical_table_not_in_mdl",
            path=path,
            field=field,
            physical_table=physical_table,
        )
    return str(model)


def _filter_storage_field_id(
    registry: CapabilityRegistry,
    field_id: str,
    resolver: str,
) -> str:
    rules = registry.snapshot.predicates.get(
        "filter_storage_fields"
    )
    if not isinstance(rules, Mapping):
        return field_id
    field_rules = rules.get(field_id)
    if not isinstance(field_rules, Mapping):
        return field_id
    storage_field = str(field_rules.get(resolver) or "").strip()
    return storage_field or field_id


def _filter_location(
    expression: str,
    *,
    default_model: str,
    default_column: str,
    dialect: str,
) -> tuple[str, str]:
    parsed = sqlglot.parse_one(expression, read=dialect)
    if not isinstance(parsed, exp.Column):
        return default_model, default_column
    return (
        str(parsed.table or default_model),
        str(parsed.name or default_column),
    )


def _normalize_filter_value(
    spec: QuerySpec,
    registry: CapabilityRegistry,
    field_capability: Any,
    op: str,
    value: Any,
    *,
    path: str,
) -> tuple[Any, bool]:
    normalized_op = op.lower()
    if (
        normalized_op in {"in", "not_in"}
        and not isinstance(value, (list, tuple, set, frozenset))
    ):
        value = (value,)
    pile_columns = {
        str(item)
        for item in (
            registry.snapshot.predicates.get("pile_columns") or ()
        )
    }
    filter_expression = str(
        field_capability.filter_expression
        or field_capability.expression
        or ""
    )
    column = filter_expression.rsplit(".", 1)[-1].strip("` ")
    if column not in pile_columns:
        return (
            _coerce_numeric_value(
                value,
                field_capability.data_type,
            ),
            False,
        )

    values = (
        tuple(value)
        if isinstance(value, (list, tuple, set, frozenset))
        else (value,)
    )
    parsed = tuple(pile_meter(item) for item in values)
    if any(item is None for item in parsed):
        _raise_contract(
            spec,
            "invalid_pile_value",
            path=path,
            field=field_capability.field_id,
            values=values,
        )
    if normalized_op == "between":
        if len(parsed) != 2:
            _raise_contract(
                spec,
                "invalid_pile_range",
                path=path,
                field=field_capability.field_id,
                values=values,
            )
        return parsed, True
    if normalized_op in {"in", "not_in"}:
        return parsed, False
    if len(parsed) == 1:
        return parsed[0], False
    return parsed, False


def _coerce_numeric_value(value: Any, data_type: str) -> Any:
    normalized_type = str(data_type or "").lower()
    is_numeric = any(
        token in normalized_type
        for token in (
            "decimal",
            "double",
            "float",
            "int",
            "number",
            "numeric",
        )
    )
    if not is_numeric:
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_coerce_numeric_value(item, data_type) for item in value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _normalize_expression(
    spec: QuerySpec,
    snapshot: MdlSnapshot,
    expression: str,
    *,
    root_model: str,
    target_model: str,
    path: str,
    field: str,
) -> str:
    normalized, _models = _parse_and_normalize_expression(
        spec,
        snapshot,
        expression,
        root_model=root_model,
        target_model=target_model,
        path=path,
        field=field,
    )
    return normalized


def _normalize_metric_expression(
    spec: QuerySpec,
    snapshot: MdlSnapshot,
    expression: str,
    *,
    root_model: str,
    path: str,
) -> tuple[str, set[str]]:
    return _parse_and_normalize_expression(
        spec,
        snapshot,
        expression,
        root_model=root_model,
        target_model=root_model,
        path=path,
        field="",
    )


def _parse_and_normalize_expression(
    spec: QuerySpec,
    snapshot: MdlSnapshot,
    expression: str,
    *,
    root_model: str,
    target_model: str,
    path: str,
    field: str,
) -> tuple[str, set[str]]:
    text = str(expression or "").replace("{alias}", root_model)
    for model in sorted(snapshot.models, key=len, reverse=True):
        text = text.replace(f"{{{model}}}", model)
    if "{" in text or "}" in text:
        _raise_contract(
            spec,
            "unsupported_expression_placeholder",
            path=path,
            field=field,
            expression=expression,
        )
    try:
        parsed = sqlglot.parse_one(f"SELECT {text}", read="mysql")
    except sqlglot.errors.ParseError:
        _raise_contract(
            spec,
            "expression_parse_failed",
            path=path,
            field=field,
            expression=expression,
        )
    if not isinstance(parsed, exp.Select) or len(parsed.expressions) != 1:
        _raise_contract(
            spec,
            "expression_parse_failed",
            path=path,
            field=field,
            expression=expression,
        )
    parsed_expression = parsed.expressions[0]
    columns = list(parsed_expression.find_all(exp.Column))
    unknown_tables = {
        str(column.table)
        for column in columns
        if column.table and str(column.table) not in snapshot.models
    }
    if len(unknown_tables) > 1:
        _raise_contract(
            spec,
            "unresolved_expression_models",
            path=path,
            field=field,
            models=sorted(unknown_tables),
        )
    for column in columns:
        qualifier = str(column.table or "")
        if not qualifier or qualifier in unknown_tables:
            column.set("table", exp.to_identifier(target_model))
    models = {
        str(column.table)
        for column in columns
        if str(column.table)
    }
    models.add(target_model)
    return parsed_expression.sql(dialect="mysql"), models


def _effective_output_ids(
    spec: QuerySpec,
    dimensions: tuple[str, ...],
    metrics: tuple[str, ...],
) -> tuple[str, ...]:
    if spec.output_projection:
        projected = tuple(
            _base_id(item) for item in spec.output_projection
        )
        values = (*projected, *dimensions, *metrics)
    elif spec.view in _METRIC_ONLY_VIEWS:
        values = metrics
    elif spec.view in _GROUPED_VIEWS:
        values = (*dimensions, *metrics)
    elif dimensions:
        values = dimensions
    else:
        values = ()
    return tuple(dict.fromkeys(value for value in values if value))


def _base_id(value: str) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _contract_digest(contract: MdlQueryContract) -> str:
    payload = {
        "plan_id": contract.plan_id,
        "node_id": contract.node_id,
        "query_spec_hash": contract.query_spec_hash,
        "capability_snapshot_hash": contract.capability_snapshot_hash,
        "mdl_fingerprint": contract.mdl_fingerprint,
        "subject": contract.subject,
        "view": contract.view,
        "root_model": contract.root_model,
        "field_bindings": {
            key: _jsonable(value)
            for key, value in contract.field_bindings.items()
        },
        "filters": [_jsonable(item) for item in contract.filters],
        "dimensions": list(contract.dimensions),
        "metrics": [_jsonable(item) for item in contract.metrics],
        "outputs": [_jsonable(item) for item in contract.outputs],
        "group_by": list(contract.group_by),
        "order_by": [_jsonable(item) for item in contract.order_by],
        "limit": contract.limit,
        "required_assertions": list(contract.required_assertions),
        "allowed_models": list(contract.allowed_models),
        "required_relationship_ids": list(
            contract.required_relationship_ids
        ),
        "intermediate_models": list(contract.intermediate_models),
        "mandatory_predicates": [
            _jsonable(item) for item in contract.mandatory_predicates
        ],
        "output_aliases": dict(contract.output_aliases),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _jsonable(item)
            for key, item in value.__dict__.items()
        }
    return value


def _raise_contract(
    spec: QuerySpec,
    reason: str,
    **evidence: Any,
) -> None:
    raise MdlSqlContractError(
        "contract_build_failed",
        {
            "reason": reason,
            "spec_id": spec.id,
            "subject": spec.subject,
            "view": spec.view,
            **evidence,
        },
    )
