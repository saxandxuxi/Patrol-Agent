from __future__ import annotations

from typing import Any

from ..services.resource_loader import load_capability_file


def _normalize(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


def _projected_fields(spec: Any) -> list[str]:
    return list(
        dict.fromkeys(
            str(field).rsplit(".", 1)[-1]
            for field in (
                *spec.dimensions,
                *spec.metrics,
                *spec.output_projection,
            )
        )
    )


def _logical_fields(spec: Any) -> list[str]:
    # 排序字段只参与精确匹配，不占结果列的位置：
    # 之前把它算进"逻辑字段"后，列表查询（1 列维度 + 1 个排序字段）
    # 会被判成"字段数与列数不一致"，位置兜底映射直接失效。
    return list(
        dict.fromkeys(
            [
                *_projected_fields(spec),
                *(
                    str(order.get("field")).rsplit(".", 1)[-1]
                    for order in spec.order_by
                    if isinstance(order, dict) and order.get("field")
                ),
            ]
        )
    )


def result_field_map(
    spec: Any,
    columns: tuple[str, ...] | list[str],
) -> dict[str, str]:
    aliases = (
        load_capability_file("result_words.yaml", {}) or {}
    ).get("field_aliases") or {}
    normalized_columns = {
        _normalize(column): str(column)
        for column in columns
    }
    logical = _logical_fields(spec)
    mapped: dict[str, str] = {}
    for field in logical:
        candidates = [
            field,
            *[
                str(item)
                for item in aliases.get(field) or ()
            ],
        ]
        match = next(
            (
                normalized_columns[_normalize(candidate)]
                for candidate in candidates
                if _normalize(candidate) in normalized_columns
            ),
            "",
        )
        if match:
            mapped[field] = match
    projected = _projected_fields(spec)
    if len(projected) == len(columns):
        for field, column in zip(projected, columns):
            mapped.setdefault(field, str(column))
    return mapped
