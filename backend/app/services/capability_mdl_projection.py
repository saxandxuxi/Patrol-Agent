from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .resource_loader import load_capability_file


def enum_definitions() -> dict[tuple[str, str], Mapping[str, Any]]:
    raw = load_capability_file("enums.yaml", {}) or {}
    definitions: dict[tuple[str, str], Mapping[str, Any]] = {}
    for value in raw.values():
        if not isinstance(value, Mapping):
            continue
        table = str(value.get("table") or "")
        column = str(value.get("column") or "")
        if table and column:
            definitions[(table, column)] = value
    return definitions


def enum_code_map(table: str, column: str) -> dict[str, str]:
    definition = enum_definitions().get((table, column)) or {}
    return {
        str(code): str(
            item.get("display")
            or item.get("label")
            or code
        )
        for code, item in (definition.get("values") or {}).items()
        if isinstance(item, Mapping)
    }


def enum_label(table: str, column: str) -> str:
    definition = enum_definitions().get((table, column)) or {}
    return str(definition.get("label") or "")


def enum_display_column(table: str, column: str) -> str:
    definition = enum_definitions().get((table, column)) or {}
    return str(definition.get("display_column") or "")


def enum_case_expression(
    table: str,
    column: str,
    *,
    qualifier: str = "",
) -> str:
    source = (
        f"{qualifier}.{column}"
        if qualifier
        else column
    )
    labels = enum_code_map(table, column)
    clauses = " ".join(
        f"WHEN {_sql_string(code)} THEN {_sql_string(label)}"
        for code, label in labels.items()
    )
    return (
        f"CASE {source} {clauses} "
        f"ELSE COALESCE(NULLIF({source}, ''), '未填写') END"
        if clauses
        else source
    )


def enum_calculated_columns(table: str) -> list[dict[str, str]]:
    columns: list[dict[str, str]] = []
    for (enum_table, column), definition in enum_definitions().items():
        if enum_table != table:
            continue
        display_column = str(definition.get("display_column") or "")
        if not display_column:
            continue
        columns.append(
            {
                "name": display_column,
                "type": "VARCHAR",
                "expression": enum_case_expression(table, column),
                "description": str(
                    definition.get("label") or display_column
                ),
            }
        )
    return columns


def table_label(kind: str, table: str) -> str:
    raw = load_capability_file("field_overrides.yaml", {}) or {}
    kind_config = raw.get(kind) if isinstance(raw, Mapping) else {}
    tables = (
        kind_config.get("tables")
        if isinstance(kind_config, Mapping)
        else {}
    )
    entry = tables.get(table) if isinstance(tables, Mapping) else {}
    return str(
        entry.get("label")
        if isinstance(entry, Mapping)
        else ""
    ) or str(table)


def model_description(
    kind: str,
    table: str,
    business_description: str,
) -> str:
    label = table_label(kind, table)
    detail = " ".join(str(business_description or "").split())
    if not detail:
        return label
    if detail == label:
        return label
    return f"{label}：{detail}"


def _sql_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"
