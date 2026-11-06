from __future__ import annotations

from typing import Any

from ..numbers import as_decimal


def select_boundary_row(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    field: str,
    *,
    lower_is_better: bool,
) -> dict[str, Any] | None:
    values = [
        row
        for row in rows
        if as_decimal(row.get(field)) is not None
    ]
    if not values:
        return None
    selector = min if lower_is_better else max
    return selector(
        values,
        key=lambda row: as_decimal(row.get(field)),
    )


def render_extreme_conclusion(
    pairs: list[tuple[str, float]],
    label_text: str = "数量",
    partial: bool = False,
    unit: str = "",
    prefix: str = "",
    with_metric_label: bool = False,
) -> str:
    if len(pairs) < 2:
        return ""
    top = max(pairs, key=lambda item: item[1])
    top_name = (
        f"{top[0]}的{label_text}"
        if with_metric_label
        else str(top[0])
    )
    if partial:
        body = f"{top[0]}的{label_text}最多（{top[1]:g}{unit}）"
        return f"（结论：{prefix}{body}。）"
    bottom = min(pairs, key=lambda item: item[1])
    if top[1] == bottom[1]:
        body = (
            f"{len(pairs)} 个{label_text}一样多，都是 "
            f"{top[1]:g}{unit}"
        )
        return f"（结论：{prefix}{body}。）"
    if bottom[1] == 0:
        body = (
            f"{top_name}最多（{top[1]:g}{unit}），"
            f"{bottom[0]}没有记录"
        )
        return f"（结论：{prefix}{body}。）"
    body = (
        f"{top_name}最多（{top[1]:g}{unit}），"
        f"{bottom[0]}最少（{bottom[1]:g}{unit}），"
        f"相差 {top[1] - bottom[1]:g}{unit}"
    )
    return f"（结论：{prefix}{body}。）"
