from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..capability_registry import CapabilityRegistry
from ..contracts import ExecutionObservation
from ..result_projection import result_field_map


_RECORD_COUNT_METRICS = frozenset(
    {"count", "count_task", "task_count", "record_count"}
)
_MISSING_VALUES = frozenset({"", "NULL", "NONE"})


def _metric_label(
    registry: CapabilityRegistry | None,
    subject: str,
    metric_id: str,
) -> str:
    if registry is not None:
        metric = registry.snapshot.metrics.get(subject, {}).get(metric_id)
        if metric is not None and metric.label:
            return str(metric.label)
    return str(metric_id)


def zero_aggregate_observation(
    spec: Any,
    observation: ExecutionObservation,
    registry: CapabilityRegistry | None,
) -> ExecutionObservation:
    """Materialize an empty aggregate without dropping requested metrics."""

    source_rows = list(observation.result_rows or observation.preview_rows)
    source_row = dict(source_rows[0]) if source_rows else {}
    columns = list(observation.columns)
    for key in source_row:
        if key not in columns:
            columns.append(str(key))

    field_map = result_field_map(spec, columns)
    metric_columns: dict[str, str] = {}
    for raw_metric in spec.metrics:
        metric_id = str(raw_metric).rsplit(".", 1)[-1]
        column = field_map.get(metric_id)
        if not column:
            column = _metric_label(registry, spec.subject, metric_id)
        if column not in columns:
            columns.append(column)
        metric_columns[metric_id.lower()] = column

    row = {str(key): value for key, value in source_row.items()}
    for column in columns:
        value = row.get(column)
        if isinstance(value, str) and value.strip().upper() in _MISSING_VALUES:
            row[column] = None
    for metric_id, column in metric_columns.items():
        if metric_id in _RECORD_COUNT_METRICS:
            row[column] = "0"
        elif column not in row:
            row[column] = None

    return replace(
        observation,
        columns=tuple(columns),
        row_count=1,
        preview_rows=(row,),
        result_rows=(row,),
        rows_fetched=1,
    )
