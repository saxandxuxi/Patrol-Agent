from __future__ import annotations

import re
from typing import Any


_PILE = re.compile(r"K?(\d+)(?:\+(\d+))?", re.IGNORECASE)
_PILE_RANGE = re.compile(
    r"(K?\d+(?:\+\d+)?)\s*(?:至|到|~|-)\s*(K?\d+(?:\+\d+)?)",
    re.IGNORECASE,
)


def pile_meter(value: Any) -> int | None:
    matched = _PILE.fullmatch(str(value or "").strip())
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
