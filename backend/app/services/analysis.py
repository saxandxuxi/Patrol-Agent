from __future__ import annotations

import re
from typing import Any

import pandas as pd

from ..config import settings
from .logging_service import log_error


_PILE_PATTERN = re.compile(r"[Kk]\s*(\d+)\s*\+\s*(\d+)")


def _coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for column in df.columns:
        converted = pd.to_numeric(df[column], errors="coerce")
        if converted.notna().mean() >= settings.analysis_numeric_ratio and converted.notna().any():
            df[column] = converted
    return df


def _is_time_column(column: str, series: pd.Series) -> bool:
    name = str(column).lower()
    if any(token in name for token in ("time", "date", "时间", "日期")):
        return True
    if pd.api.types.is_numeric_dtype(series):
        return False
    converted = pd.to_datetime(series, errors="coerce", format="mixed")
    return converted.notna().mean() >= settings.analysis_numeric_ratio


def _is_time_name(column: Any) -> bool:
    name = str(column).lower()
    return any(token in name for token in ("time", "date", "时间", "日期"))


def _parse_pile(value: Any) -> int | None:
    match = _PILE_PATTERN.search(str(value))
    if match:
        return int(match.group(1)) * 1000 + int(match.group(2))
    match = re.search(r"(\d+)", str(value))
    return int(match.group(1)) if match else None


def _detail_chart(columns: list[str], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """明细结果没有数值指标列，改为对桩号/时间/类型做聚合出图。"""
    try:
        df = pd.DataFrame(rows, columns=columns)

        pile_col = next(
            (col for col in df.columns if "pile" in str(col).lower() or "桩号" in str(col)),
            None,
        )
        if pile_col:
            piles = pd.to_numeric(df[pile_col].map(_parse_pile), errors="coerce").dropna()
            if len(piles) >= 2:
                buckets = (piles // 100).astype(int) * 100
                counts = buckets.value_counts().sort_index()
                if len(counts) > 1:
                    return {
                        "type": "bar",
                        "x": [f"K{int(k) // 1000}+{int(k) % 1000:03d}" for k in counts.index],
                        "series": [{"name": "处数", "data": counts.tolist()}],
                    }

        time_col = next((col for col in df.columns if _is_time_name(col)), None)
        if time_col:
            series = pd.to_datetime(df[time_col], errors="coerce", format="mixed")
            if series.notna().mean() >= settings.analysis_numeric_ratio:
                if series.dt.date.nunique() > 1:
                    counts = series.dt.date.value_counts().sort_index()
                    return {
                        "type": "line",
                        "x": [str(key) for key in counts.index],
                        "series": [{"name": "数量", "data": counts.tolist()}],
                    }
                if series.dt.hour.nunique() > 1:
                    counts = series.dt.hour.value_counts().sort_index()
                    return {
                        "type": "line",
                        "x": [f"{int(key):02d}:00" for key in counts.index],
                        "series": [{"name": "数量", "data": counts.tolist()}],
                    }

        category_col = next(
            (
                col
                for col in df.columns
                if str(col).lower() in {"type", "damage_type", "problem_type"} or "类型" in str(col)
            ),
            None,
        )
        if category_col:
            counts = df[category_col].astype(str).value_counts()
            if len(counts) > 1:
                return {
                    "type": "pie" if len(counts) <= 8 else "bar",
                    "x": counts.index.tolist(),
                    "series": [{"name": "数量", "data": counts.tolist()}],
                }
    except Exception as exc:  # noqa: BLE001
        # 明细图推不出来不致命（返回 None = 不出图），但要留痕，
        # 否则"这条结果没有图"和"内部异常"分不出来
        log_error("analysis", "detail_chart_failed", exc, columns=len(columns or []), rows=len(rows or []))
        return None
    return None


def analyze(
    result: dict[str, Any],
    intent: str,
    shape: str | None = None,
) -> dict[str, Any] | None:
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if not columns or not rows:
        return None

    if shape in {"list", "photo"}:
        return _detail_chart(columns, rows)

    if intent != "analysis":
        return None
    if len(columns) >= 4:
        return None

    try:
        df = _coerce_numeric(pd.DataFrame(rows, columns=columns))
    except Exception as exc:  # noqa: BLE001
        log_error("analysis", "frame_build_failed", exc, columns=len(columns or []), rows=len(rows or []))
        return None

    numeric_cols = [col for col in df.columns if pd.api.types.is_numeric_dtype(df[col])]
    if not numeric_cols:
        return None

    value_col = numeric_cols[0]
    time_col = next((col for col in df.columns if col != value_col and _is_time_column(col, df[col])), None)
    category_col = next(
        (col for col in df.columns if col not in numeric_cols and col != time_col),
        None,
    )

    if time_col:
        sub = df[[time_col, value_col]].dropna()
        return {
            "type": "line",
            "x": sub[time_col].astype(str).tolist(),
            "series": [{"name": value_col, "data": sub[value_col].tolist()}],
        }

    if category_col:
        sub = df[[category_col, value_col]].dropna()
        chart_type = "pie" if shape == "distribution" and len(sub) <= 8 else "bar"
        return {
            "type": chart_type,
            "x": sub[category_col].astype(str).tolist(),
            "series": [{"name": value_col, "data": sub[value_col].tolist()}],
        }

    # 只有一个数值列时，用序号作为分类轴。
    return {
        "type": "bar",
        "x": df.index.astype(str).tolist(),
        "series": [{"name": value_col, "data": df[value_col].tolist()}],
    }
