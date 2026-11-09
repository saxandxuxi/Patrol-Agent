from __future__ import annotations

import json
from typing import Any

from ..numbers import as_decimal


def _base_field(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _projected_fields(dependency: dict[str, Any]) -> list[str]:
    spec = dependency.get("spec")
    if not isinstance(spec, dict):
        return []
    return list(
        dict.fromkeys(
            _base_field(field)
            for field in (
                *spec.get("dimensions", ()),
                *spec.get("metrics", ()),
                *spec.get("output_projection", ()),
            )
            if _base_field(field)
        )
    )


def _canonical_dataset(
    dependency: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]], dict[str, str]]:
    """把展示列对齐回 QuerySpec 的逻辑字段。

    空结果集经常没有物理列名，而非空结果集只有中文展示列名。差集必须按
    `problem_number` 这类逻辑身份比较，不能让结果是否为空改变字段身份。
    """

    rows = [
        dict(row)
        for row in dependency.get("rows") or []
        if isinstance(row, dict) and row
    ]
    labels = dependency.get("field_labels")
    labels = labels if isinstance(labels, dict) else {}
    display_to_field = {
        str(display): _base_field(field)
        for field, display in labels.items()
        if str(display).strip() and _base_field(field)
    }
    field_to_display = {
        _base_field(field): str(display)
        for field, display in labels.items()
        if str(display).strip() and _base_field(field)
    }
    raw_columns = (
        list(rows[0])
        if rows
        else [str(item) for item in dependency.get("columns") or []]
    )
    columns = [
        display_to_field.get(str(column), _base_field(column))
        for column in raw_columns
        if _base_field(column)
    ]
    for field in _projected_fields(dependency):
        if field not in columns:
            columns.append(field)
    canonical_rows = []
    for row in rows:
        canonical_rows.append(
            {
                display_to_field.get(
                    str(column),
                    _base_field(column),
                ): value
                for column, value in row.items()
                if _base_field(column)
            }
        )
    for column, field in display_to_field.items():
        field_to_display.setdefault(field, column)
    return columns, canonical_rows, field_to_display


def compare_datasets(
    dependencies: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        dependency
        for dependency in dependencies
        if isinstance(dependency.get("rows"), list)
    ]
    if len(candidates) < 2:
        return None
    left_columns, left_rows, left_labels = _canonical_dataset(candidates[0])
    right_columns, right_rows, right_labels = _canonical_dataset(candidates[1])
    common_columns = [
        column for column in left_columns if column in right_columns
    ]
    if not common_columns:
        return None
    if not left_rows and not right_rows:
        return {
            "reply": "两组均无匹配记录，未发现可比较的数据差异。",
            "source": "calc",
            "engine": "post_calc",
            "accepted": True,
            "columns": common_columns,
            "rows": [],
        }

    def normalized(value: Any) -> str:
        return str("" if value is None else value).strip()

    def comparable(value: Any) -> str:
        number = as_decimal(value)
        return format(number.normalize(), "f") if number is not None else normalized(value)

    key_column = ""
    for column in common_columns:
        left_keys = [normalized(row.get(column)) for row in left_rows]
        right_keys = [normalized(row.get(column)) for row in right_rows]
        if (
            (not left_keys or all(left_keys))
            and (not right_keys or all(right_keys))
            and len(set(left_keys)) == len(left_keys)
            and len(set(right_keys)) == len(right_keys)
        ):
            key_column = column
            break
    if not key_column:
        return None
    compare_columns = [
        column for column in common_columns if column != key_column
    ]
    left_by_key = {
        normalized(row.get(key_column)): row for row in left_rows
    }
    right_by_key = {
        normalized(row.get(key_column)): row for row in right_rows
    }
    output_rows: list[dict[str, Any]] = []
    added = sorted(set(right_by_key) - set(left_by_key))
    removed = sorted(set(left_by_key) - set(right_by_key))
    changed = 0
    for key in added:
        output_rows.append(
            {
                left_labels.get(key_column)
                or right_labels.get(key_column)
                or key_column: key,
                "差异类型": "新增",
                "字段": "",
                "第一组": "",
                "第二组": json.dumps(
                    right_by_key[key],
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )
    for key in removed:
        output_rows.append(
            {
                left_labels.get(key_column)
                or right_labels.get(key_column)
                or key_column: key,
                "差异类型": "缺失",
                "字段": "",
                "第一组": json.dumps(
                    left_by_key[key],
                    ensure_ascii=False,
                    default=str,
                ),
                "第二组": "",
            }
        )
    for key in sorted(set(left_by_key) & set(right_by_key)):
        left = left_by_key[key]
        right = right_by_key[key]
        for column in compare_columns:
            before = comparable(left.get(column))
            after = comparable(right.get(column))
            if before == after:
                continue
            changed += 1
            output_rows.append(
                {
                    left_labels.get(key_column)
                    or right_labels.get(key_column)
                    or key_column: key,
                    "差异类型": "字段变化",
                    "字段": (
                        left_labels.get(column)
                        or right_labels.get(column)
                        or column
                    ),
                    "第一组": before,
                    "第二组": after,
                }
            )
    key_label = (
        left_labels.get(key_column)
        or right_labels.get(key_column)
        or key_column
    )
    reply = (
        f"按“{key_label}”匹配两组数据："
        f"新增 {len(added)}，缺失 {len(removed)}，字段变化 {changed}"
    )
    if not output_rows:
        reply += "；未发现差异"
    return {
        "reply": reply + "。",
        "source": "calc",
        "engine": "post_calc",
        "accepted": True,
        "columns": [
            key_label,
            "差异类型",
            "字段",
            "第一组",
            "第二组",
        ],
        "rows": output_rows[:500],
    }


def difference_conclusion(
    rows: list[dict[str, Any]],
    *,
    force: bool = False,
) -> str:
    if not force or len(rows) != 2:
        return ""
    columns = list(rows[0])
    numeric = [
        column
        for column in columns
        if all(as_decimal(row.get(column)) is not None for row in rows)
    ]
    if not numeric:
        return ""
    metric = numeric[0]
    label = next(
        (
            column
            for column in columns
            if column not in numeric
            and all(str(row.get(column) or "").strip() for row in rows)
        ),
        "",
    )
    if not label:
        return ""
    left, right = rows
    left_value = as_decimal(left.get(metric))
    right_value = as_decimal(right.get(metric))
    if left_value is None or right_value is None:
        return ""
    if left_value == right_value:
        return f"（两者一样多：各 {left_value:g}。）"
    if left_value > right_value:
        bigger, top, bottom = str(left.get(label)), left_value, right_value
    else:
        bigger, top, bottom = str(right.get(label)), right_value, left_value
    return f"（{bigger} 更多：{top:g} vs {bottom:g}，多 {top - bottom:g}。）"
