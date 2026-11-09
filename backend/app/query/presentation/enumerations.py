from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import product
from typing import Any

from ..capability_registry import CapabilityRegistry
from ..contracts import ExecutionObservation
from ..result_projection import result_field_map


_TIME_FIELDS = frozenset({"collection_time", "create_time"})
_MONTH_DIMENSIONS = frozenset(
    {"month", "collection_time_month", "create_time_month"}
)


@dataclass(frozen=True)
class _EnumDimension:
    field_id: str
    label: str
    column: str
    labels: tuple[str, ...]


def _normalized(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _dimension_filter(spec: Any, dimension_id: str) -> Any | None:
    for item in spec.filters:
        if str(item.field).rsplit(".", 1)[-1] != dimension_id:
            continue
        return item
    return None


def _wanted_values(spec: Any, dimension_id: str) -> tuple[str, ...]:
    item = _dimension_filter(spec, dimension_id)
    if item is None:
        return ()
    value = item.storage_value
    return tuple(
        _normalized(entry)
        for entry in (
            value if isinstance(value, (list, tuple)) else [value]
        )
        if _normalized(entry)
    )


def _as_datetime(value: Any) -> datetime | None:
    text = _normalized(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _explicit_time_bounds(spec: Any) -> tuple[datetime, datetime] | None:
    time_filters = [
        item
        for item in spec.filters
        if str(item.field).rsplit(".", 1)[-1] in _TIME_FIELDS
    ]
    for item in time_filters:
        if item.op != "between" or not isinstance(
            item.storage_value,
            (list, tuple),
        ):
            continue
        values = list(item.storage_value)
        if len(values) != 2:
            continue
        start, end = (_as_datetime(values[0]), _as_datetime(values[1]))
        if start is not None and end is not None and start < end:
            return start, end
    lower = next((item for item in time_filters if item.op == ">="), None)
    upper = next(
        (
            item
            for item in time_filters
            if item.op in {"<", "<="}
        ),
        None,
    )
    if lower is None or upper is None:
        return None
    start = _as_datetime(lower.storage_value)
    end = _as_datetime(upper.storage_value)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _month_labels(spec: Any) -> tuple[str, ...]:
    bounds = _explicit_time_bounds(spec)
    if bounds is None:
        return ()
    start, end = bounds
    year, month = start.year, start.month
    labels: list[str] = []
    while (year, month) < (end.year, end.month):
        labels.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
        if len(labels) > 366:
            return ()
    return tuple(labels)


def _labels_for_values(
    enum: Any,
    wanted: tuple[str, ...],
    excluded_labels: frozenset[str],
) -> tuple[str, ...]:
    labels: list[str] = []
    for wanted_value in wanted:
        for key, value in enum.values.items():
            tokens = {
                _normalized(token)
                for token in (key, value.code, value.label, value.display)
                if _normalized(token)
            }
            if wanted_value not in tokens:
                continue
            label = _normalized(value.display or value.label or key)
            if (
                label
                and label not in excluded_labels
                and label not in labels
            ):
                labels.append(label)
    return tuple(labels)


def _labels_for_explicit_values(
    enum: Any,
    wanted: tuple[str, ...],
    excluded_labels: frozenset[str],
    binding: Any | None = None,
) -> tuple[str, ...]:
    """Resolve a closed dimension from values already fixed by the plan.

    Entity dimensions such as disease type are validated by the dictionary at
    QuerySpec time, but do not have a generic enum catalog entry. Their
    explicit filter values are still a deterministic domain and can be
    combined with another closed enum dimension.
    """

    labels = (
        _labels_for_values(enum, wanted, excluded_labels)
        if enum is not None
        else ()
    )
    if labels:
        return labels
    if binding is not None:
        display = binding.display_value
        display_values = (
            tuple(
                _normalized(entry)
                for entry in display
                if _normalized(entry)
            )
            if isinstance(display, (list, tuple))
            else (_normalized(display),)
            if len(wanted) == 1
            else ()
        )
        if display_values and display_values != wanted:
            return tuple(
                label
                for label in display_values
                if label not in excluded_labels and label not in labels
            )
    return tuple(
        label
        for value in wanted
        if (label := _normalized(value))
        and label not in excluded_labels
        and label not in labels
    )


def _labels_for_enum_domain(
    enum: Any,
    excluded_labels: frozenset[str],
) -> tuple[str, ...]:
    labels: list[str] = []
    for value in enum.values.values():
        label = _normalized(value.display or value.label or value.code)
        if (
            label
            and label not in excluded_labels
            and label not in labels
        ):
            labels.append(label)
    return tuple(labels)


def _excluded_enum_labels(registry: CapabilityRegistry) -> frozenset[str]:
    semantics = registry.snapshot.query_shapes.get("planner_semantics") or {}
    completion = semantics.get("enum_dimension_completion") or {}
    return frozenset(
        _normalized(item)
        for item in completion.get("excluded_values") or ()
        if _normalized(item)
    )


def _metric_columns(
    spec: Any,
    columns: list[str],
    registry: CapabilityRegistry,
) -> dict[str, str]:
    field_map = result_field_map(spec, columns)
    metric_capabilities = registry.snapshot.metrics.get(spec.subject, {})
    mapped: dict[str, str] = {}
    for raw_metric in spec.metrics:
        metric_id = str(raw_metric).rsplit(".", 1)[-1]
        column = field_map.get(metric_id)
        if not column:
            capability = metric_capabilities.get(metric_id)
            column = (
                str(capability.label)
                if capability is not None and capability.label
                else metric_id
            )
        if column not in columns:
            columns.append(column)
        mapped[metric_id.lower()] = column
    return mapped


def _zero_for_missing_group(metric_id: str) -> bool:
    return metric_id == "count" or metric_id.startswith(("total_", "sum_"))


def missing_explicit_top_group_labels(
    spec: Any,
    observation: ExecutionObservation,
    registry: CapabilityRegistry | None,
) -> list[str]:
    """Explain requested closed groups that produced no ranked row.

    ``top_groups`` ranks real rows. Materializing a synthetic zero row after
    ranking can create a false first place, so missing explicit groups are
    reported as an observation instead of being inserted into the result.
    """

    if registry is None or spec.view != "top_groups" or not spec.dimensions:
        return []
    rows = [
        dict(row)
        for row in (observation.result_rows or observation.preview_rows)
        if isinstance(row, dict)
    ]
    if not rows:
        return []

    columns = list(observation.columns)
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    field_map = result_field_map(spec, columns)
    excluded_labels = _excluded_enum_labels(registry)
    completion_config = registry.snapshot.query_shapes.get(
        "planner_semantics",
        {},
    ).get("enum_dimension_completion") or {}
    default_complete_fields = (
        completion_config.get("default_complete_fields") or {}
        if isinstance(completion_config, Mapping)
        else {}
    )
    configured_complete = {
        _normalized(item)
        for item in default_complete_fields.get(spec.subject, ()) or ()
    }
    missing: list[str] = []
    for raw_dimension in spec.dimensions:
        dimension_id = str(raw_dimension).rsplit(".", 1)[-1]
        explicit_filter = _dimension_filter(spec, dimension_id)
        field = registry.snapshot.fields.get(spec.subject, {}).get(dimension_id)
        enum = None
        if field is not None and field.physical_table and field.physical_column:
            enum = registry.snapshot.enums.get(
                (field.physical_table, field.physical_column)
            )
        if (
            explicit_filter is not None
            and explicit_filter.op in {"=", "in"}
        ):
            expected = _labels_for_explicit_values(
                enum,
                _wanted_values(spec, dimension_id),
                excluded_labels,
                explicit_filter,
            )
        elif enum is not None and dimension_id in configured_complete:
            expected = _labels_for_enum_domain(enum, excluded_labels)
        else:
            continue
        if not expected:
            continue
        column = field_map.get(dimension_id) or str(
            field.label
            if field is not None and field.label
            else dimension_id
        )
        observed = {
            _normalized(row.get(column))
            for row in rows
            if _normalized(row.get(column))
        }
        label = column
        for value in expected:
            if value not in observed:
                item = f"{label} {value}"
                if item not in missing:
                    missing.append(item)
    return missing


def complete_explicit_enum_groups(
    spec: Any,
    observation: ExecutionObservation,
    registry: CapabilityRegistry | None,
    *,
    scope_has_records: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Complete deterministic closed domains without inventing unseen values.

    Explicit plan values and registered closed enums define a domain directly.
    When one dimension is an exhaustive closed domain, categories already
    observed on the other side supply that side's real domain, which allows a
    counterfactual zero row without asking the model to invent category names.
    """

    rows = [dict(row) for row in observation.result_rows or observation.preview_rows]
    if (
        registry is None
        or spec.view != "distribution"
        or not spec.dimensions
        or not spec.metrics
        or (not rows and not scope_has_records)
    ):
        return rows, []

    columns = list(observation.columns)
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    field_map = result_field_map(spec, columns)
    completion_config = registry.snapshot.query_shapes.get(
        "planner_semantics",
        {},
    ).get("enum_dimension_completion") or {}
    default_complete_fields = (
        completion_config.get("default_complete_fields") or {}
        if isinstance(completion_config, Mapping)
        else {}
    )
    configured_complete = {
        _normalized(item)
        for item in default_complete_fields.get(spec.subject, ()) or ()
    }
    complete_domain = (
        "complete_enum_dimensions" in spec.required_assertions
    )
    excluded_labels = _excluded_enum_labels(registry)
    dimensions: list[_EnumDimension] = []
    raw_dimension_ids = [
        str(raw_dimension).rsplit(".", 1)[-1]
        for raw_dimension in spec.dimensions
    ]
    has_exhaustive_dimension = complete_domain or any(
        dimension_id in configured_complete
        for dimension_id in raw_dimension_ids
    )
    for raw_dimension in spec.dimensions:
        dimension_id = str(raw_dimension).rsplit(".", 1)[-1]
        explicit_filter = _dimension_filter(spec, dimension_id)
        wanted = _wanted_values(spec, dimension_id)
        field = registry.snapshot.fields.get(spec.subject, {}).get(dimension_id)
        enum = None
        if field is not None and field.physical_table and field.physical_column:
            enum = registry.snapshot.enums.get(
                (field.physical_table, field.physical_column)
            )
        labels = (
            _labels_for_explicit_values(
                enum,
                wanted,
                excluded_labels,
                explicit_filter,
            )
            if wanted
            else _labels_for_enum_domain(enum, excluded_labels)
            if enum is not None
            and (complete_domain or dimension_id in configured_complete)
            else _month_labels(spec)
            if dimension_id in _MONTH_DIMENSIONS
            else ()
        )
        column = field_map.get(dimension_id)
        if not column:
            column = str(
                field.label
                if field is not None and field.label
                else "月份"
                if dimension_id in _MONTH_DIMENSIONS
                else dimension_id
            )
            if column not in columns:
                columns.append(column)
        dimensions.append(
            _EnumDimension(
                field_id=dimension_id,
                label=str(
                    field.label
                    if field is not None and field.label
                    else "月份"
                    if dimension_id in _MONTH_DIMENSIONS
                    else dimension_id
                ),
                column=column,
                labels=labels,
            )
        )

    if has_exhaustive_dimension:
        observed_dimensions: list[_EnumDimension] = []
        for dimension in dimensions:
            if dimension.labels:
                observed_dimensions.append(dimension)
                continue
            observed = tuple(
                dict.fromkeys(
                    _normalized(row.get(dimension.column))
                    for row in rows
                    if _normalized(row.get(dimension.column))
                    and _normalized(row.get(dimension.column))
                    not in excluded_labels
                )
            )
            if not observed:
                return rows, []
            observed_dimensions.append(
                replace(dimension, labels=observed)
            )
        dimensions = observed_dimensions
    if any(not dimension.labels for dimension in dimensions):
        return rows, []

    metric_columns = _metric_columns(spec, columns, registry)
    for row in rows:
        for column in columns:
            row.setdefault(column, None)
    present = {
        tuple(_normalized(row.get(dimension.column)) for dimension in dimensions)
        for row in rows
    }
    metric_capabilities = registry.snapshot.metrics.get(spec.subject, {})
    invariant_values: dict[str, Any] = {}
    for metric_id, column in metric_columns.items():
        capability = metric_capabilities.get(metric_id)
        if capability is None or not capability.propagate_across_groups:
            continue
        values = {
            _normalized(row.get(column))
            for row in rows
            if _normalized(row.get(column)).upper() not in {"", "NULL", "NONE"}
        }
        if len(values) == 1:
            invariant_values[metric_id] = next(iter(values))

    filled: list[str] = []
    for labels in product(*(dimension.labels for dimension in dimensions)):
        if labels in present:
            continue
        row = {column: None for column in columns}
        for dimension, label in zip(dimensions, labels):
            row[dimension.column] = label
        for metric_id, column in metric_columns.items():
            row[column] = (
                0
                if _zero_for_missing_group(metric_id)
                else invariant_values.get(metric_id)
            )
        rows.append(row)
        filled.append("/".join(labels))
    return rows, filled
