from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


_TIME_FAMILIES = {
    "day": {
        "day",
        "collection_time_day",
        "create_time_day",
    },
    "week": {"week"},
    "month": {
        "month",
        "collection_time_month",
        "create_time_month",
    },
    "quarter": {"quarter", "collection_time_quarter"},
    "year": {"year", "collection_time_year"},
}
_MULTIPLIER = {"day": 1, "week": 2, "month": 3, "quarter": 4, "year": 5}


def _field_name(field: Any) -> str:
    return str(field or "").rsplit(".", 1)[-1]


def _time_family(spec: Any) -> str:
    names = {_field_name(item) for item in spec.dimensions}
    for family in sorted(
        _TIME_FAMILIES,
        key=lambda item: _MULTIPLIER[item],
        reverse=True,
    ):
        if names.intersection(_TIME_FAMILIES[family]):
            return family
    return ""


def _window(spec: Any) -> tuple[datetime, datetime] | None:
    start: datetime | None = None
    end: datetime | None = None
    for item in spec.filters:
        if _field_name(item.field) not in {"collection_time", "create_time"}:
            continue
        try:
            if (
                item.op == "between"
                and isinstance(item.storage_value, (list, tuple))
                and len(item.storage_value) == 2
            ):
                start = datetime.fromisoformat(str(item.storage_value[0]))
                end = datetime.fromisoformat(str(item.storage_value[1]))
            elif item.op in {">", ">="}:
                start = datetime.fromisoformat(str(item.storage_value))
            elif item.op in {"<", "<="}:
                end = datetime.fromisoformat(str(item.storage_value))
                if item.op == "<":
                    end -= timedelta(microseconds=1)
        except ValueError:
            continue
    if start is None or end is None or start > end:
        return None
    return start, end


def _add_months(year: int, month: int, count: int = 1) -> tuple[int, int]:
    index = year * 12 + month - 1 + count
    return index // 12, index % 12 + 1


def _expected_labels(
    family: str,
    start: datetime,
    end: datetime,
) -> list[str]:
    if family == "day":
        result = []
        current = start.date()
        while current <= end.date():
            result.append(current.isoformat())
            current += timedelta(days=1)
        return result
    if family == "week":
        result = []
        current = start.date()
        while current <= end.date():
            year, week, _ = current.isocalendar()
            result.append(f"{year:04d}-W{week:02d}")
            current += timedelta(weeks=1)
        return result
    if family == "month":
        result = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            result.append(f"{year:04d}-{month:02d}")
            year, month = _add_months(year, month)
        return result
    if family == "quarter":
        result = []
        year, quarter = start.year, (start.month - 1) // 3 + 1
        end_pair = (end.year, (end.month - 1) // 3 + 1)
        while (year, quarter) <= end_pair:
            result.append(f"{year:04d}-Q{quarter}")
            quarter += 1
            if quarter == 5:
                year += 1
                quarter = 1
        return result
    if family == "year":
        return [
            f"{year:04d}"
            for year in range(start.year, end.year + 1)
        ]
    return []


def _actual_label(family: str, value: Any) -> str:
    text = str(value or "").strip()
    pattern = {
        "day": r"(\d{4}-\d{2}-\d{2})",
        "week": r"(\d{4}-W\d{1,2})",
        "month": r"(\d{4}-\d{2})(?!-)",
        "quarter": r"(\d{4}-Q[1-4])",
        "year": r"(\d{4})",
    }.get(family, "")
    if not pattern:
        return ""
    matched = re.search(pattern, text, re.IGNORECASE)
    return matched.group(1) if matched else ""


def _partial_window_note(
    family: str,
    start: datetime,
    end: datetime,
) -> str:
    """首尾时间桶是否只覆盖了一部分周期。

    2026-10-08 至 2026-11-07 按月分桶，会得到“10月 11886、11月 1883”，
    两个桶天数相差一倍以上，直接比大小等于自欺欺人。这种窗口必须在回答里
    说明，而不是让趋势结论看起来是完整周期的对比。
    """
    partials: list[str] = []
    if family == "week":
        if start.weekday() != 0:
            partials.append("首个周桶不是从周一开始")
        if end.weekday() != 6:
            partials.append("最后一个周桶没有到周日")
    elif family == "month":
        if start.day != 1:
            partials.append(f"首月从 {start.strftime('%m-%d')} 才计入")
        next_month = _add_months(end.year, end.month)
        month_end = (
            datetime(next_month[0], next_month[1], 1) - timedelta(days=1)
        ).date()
        if end.date() != month_end:
            partials.append(f"末月只到 {end.strftime('%m-%d')}")
    elif family == "quarter":
        if start.month % 3 != 1 or start.day != 1:
            partials.append("首个季度桶不是完整季度")
        next_month = _add_months(end.year, end.month)
        month_end = (
            datetime(next_month[0], next_month[1], 1) - timedelta(days=1)
        ).date()
        if end.month % 3 != 0 or end.date() != month_end:
            partials.append("最后一个季度桶不是完整季度")
    elif family == "year":
        if (start.month, start.day) != (1, 1):
            partials.append("首年不是从 1 月 1 日开始")
        if (end.month, end.day) != (12, 31):
            partials.append("末年没有到 12 月 31 日")
    if not partials:
        return ""
    return (
        "这个窗口的首尾时间桶不完整（"
        + "、".join(partials)
        + "），各桶覆盖的天数不同，不能直接比较大小；要比较趋势请换成完整周期。"
    )


def time_window_coverage(spec: Any, rows: list[dict[str, Any]]) -> str:
    family = _time_family(spec)
    if not family:
        return ""
    if spec.view != "trend" and not (
        spec.view == "distribution"
        and family in {"day", "week", "month", "quarter", "year"}
    ):
        return ""
    window = _window(spec)
    if window is None:
        return ""
    start, end = window
    expected = _expected_labels(family, start, end)
    actual = {
        label
        for row in rows
        for value in row.values()
        if (label := _actual_label(family, value))
    }
    missing = [item for item in expected if item not in actual]
    window_text = (
        f"{start.strftime('%Y-%m-%d')} 至 {end.strftime('%Y-%m-%d')}"
    )
    if not actual:
        if spec.view == "trend":
            return f"查询窗口 {window_text} 内没有匹配记录，无法判断趋势。"
        return f"查询窗口 {window_text} 内没有匹配记录。"
    if spec.view == "distribution":
        if not missing:
            return ""
        return (
            "查询范围内以下时间桶没有匹配记录："
            + "、".join(missing)
        )
    if not missing:
        if spec.view == "trend":
            partial = _partial_window_note(family, start, end)
            return f"查询窗口 {window_text}；{partial}" if partial else ""
        return ""
    return (
        f"查询窗口 {window_text}；"
        f"实际返回时间桶为 {'、'.join(sorted(actual))}；"
        f"缺少日期或时间桶 {'、'.join(missing)}，"
        "无法据此判断完整时间窗口的趋势。"
    )
