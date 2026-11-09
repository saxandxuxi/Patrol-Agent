from __future__ import annotations

from typing import Any

from .numbers import as_decimal


def row_with_group_labels(
    dependency: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    item = dict(row)
    for key, value in (dependency.get("group_labels") or {}).items():
        item.setdefault(str(key), value)
    return item


def compact_table_evidence(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    valid_rows = [
        dict(row) for row in rows if isinstance(row, dict) and row
    ]
    if not valid_rows:
        return {
            "row_count": 0,
            "group_columns": [],
            "metrics": {},
            "group_summaries": [],
        }
    columns = list(valid_rows[0])
    numeric_columns = [
        column
        for column in columns
        if sum(
            as_decimal(row.get(column)) is not None
            for row in valid_rows
        )
        >= max(1, len(valid_rows) // 2)
    ]
    group_columns = [
        column
        for column in columns
        if column not in numeric_columns
        and any(str(row.get(column) or "").strip() for row in valid_rows)
    ]

    def group_label(row: dict[str, Any]) -> str:
        return " / ".join(
            str(row.get(column) or "").strip()
            for column in group_columns
            if str(row.get(column) or "").strip()
        )

    def metric_boundaries(
        selected_rows: list[dict[str, Any]],
    ) -> dict[
        str,
        tuple[tuple[Any, dict[str, Any]], tuple[Any, dict[str, Any]]],
    ]:
        boundaries = {}
        for column in numeric_columns:
            values = []
            for row in selected_rows:
                value = as_decimal(row.get(column))
                if value is not None:
                    values.append((value, row))
            if not values:
                continue
            boundaries[column] = (
                min(values, key=lambda item: item[0]),
                max(values, key=lambda item: item[0]),
            )
        return boundaries

    metrics: dict[str, Any] = {}
    for column, (minimum, maximum) in metric_boundaries(valid_rows).items():
        metrics[column] = {
            "min": {
                "group": group_label(minimum[1]),
                "value": float(minimum[0]),
            },
            "max": {
                "group": group_label(maximum[1]),
                "value": float(maximum[0]),
            },
        }

    group_summaries: list[dict[str, Any]] = []
    if len(group_columns) > 1 and numeric_columns:
        parent_columns = group_columns[:1]
        detail_columns = group_columns[1:]
        groups_by_parent: dict[
            tuple[str, ...],
            list[dict[str, Any]],
        ] = {}
        for row in valid_rows:
            parent_key = tuple(
                str(row.get(column) or "").strip()
                for column in parent_columns
            )
            groups_by_parent.setdefault(parent_key, []).append(row)

        groups = []
        for parent_key, selected_rows in groups_by_parent.items():
            group_metrics = {}
            for column, (minimum, maximum) in metric_boundaries(
                selected_rows
            ).items():
                group_metrics[column] = {
                    "min": {
                        "value": float(minimum[0]),
                        "row": {
                            detail: minimum[1].get(detail)
                            for detail in detail_columns
                            if minimum[1].get(detail) not in (None, "")
                        },
                    },
                    "max": {
                        "value": float(maximum[0]),
                        "row": {
                            detail: maximum[1].get(detail)
                            for detail in detail_columns
                            if maximum[1].get(detail) not in (None, "")
                        },
                    },
                }
            groups.append(
                {
                    "group": dict(zip(parent_columns, parent_key)),
                    "row_count": len(selected_rows),
                    "metrics": group_metrics,
                }
            )
        group_summaries.append(
            {
                "parent_columns": parent_columns,
                "detail_columns": detail_columns,
                "groups": groups,
            }
        )
    return {
        "row_count": len(valid_rows),
        "columns": columns,
        "group_columns": group_columns,
        "metrics": metrics,
        "group_summaries": group_summaries,
    }
