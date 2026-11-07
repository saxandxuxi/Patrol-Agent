from __future__ import annotations

import re
from typing import Any


_PILE = re.compile(r"K?(\d+)(?:\+(\d+))?", re.IGNORECASE)
_PILE_RANGE = re.compile(
    r"(K?\d+(?:\+\d+)?)\s*(?:至|到|~|-)\s*(K?\d+(?:\+\d+)?)",
    re.IGNORECASE,
)


def pile_meter(value: Any) -> int | None:
    # 数值类型是上游值解析器已经归一化好的**米**（QuerySpec 绑定的 storage_value
    # 用的就是米）。再按"公里"解析一次会把 K120 放大成 120000000（实测
    # "K120 到 K125" 被查成空结果）。0 也必须是合法里程（K0 起点）。
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    matched = _PILE.fullmatch(str(value if value is not None else "").strip())
    if matched is None:
        return None
    kilometer = int(matched.group(1))
    meter = int(matched.group(2) or 0)
    return kilometer * 1000 + meter


def pile_range(value: Any) -> tuple[int, int] | None:
    matched = _PILE_RANGE.search(str(value or ""))
    if matched is None:
        return None
    start = pile_meter(matched.group(1))
    end = pile_meter(matched.group(2))
    if start is None or end is None:
        return None
    return start, end
