from __future__ import annotations

from typing import Any

from .numbers import as_decimal


def compact_table_evidence(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    valid_rows = [
        dict(row) for row in rows if isinstance(row, dict) and row
    ]
    if not valid_rows:
        return {"row_count": 0, "group_columns": [], "metrics": {}}
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

    metrics: dict[str, Any] = {}
    for column in numeric_columns:
        values = []
        for row in valid_rows:
            value = as_decimal(row.get(column))
            if value is not None:
                values.append((value, row))
        if not values:
            continue
        minimum = min(values, key=lambda item: item[0])
        maximum = max(values, key=lambda item: item[0])
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
    return {
        "row_count": len(valid_rows),
        "columns": columns,
        "group_columns": group_columns,
        "metrics": metrics,
    }
