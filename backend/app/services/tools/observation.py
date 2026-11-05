from __future__ import annotations

import re
from typing import Any


_WEEK_LABEL = re.compile(r"(?P<year>\d{4})(?:\d{2})?年第(?P<week>\d{1,2})周")


def normalize_week_label(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = str(value or "")
    return _WEEK_LABEL.sub(
        lambda match: f"{match.group('year')}年第{int(match.group('week'))}周",
        text,
    )


def observation(
    tool: str,
    *,
    ok: bool,
    summary: str,
    data: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
    error: str | None = None,
    next_hint: str = "",
    accepted: bool | None = None,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "accepted": bool(ok) if accepted is None else bool(accepted),
        "tool": str(tool),
        # summary 是工具的完整语义产出；日志层会自行截断，不能在这里损坏最终回答。
        "summary": str(summary or ""),
        "data": data
        or {
            "row_count": 0,
            "columns": [],
            "preview_rows": [],
            "produced": {},
            "truncated": False,
        },
        "coverage": coverage
        or {
            "asked_range": {"start": "", "end": ""},
            "actual_range": {"start": "", "end": ""},
            "empty": False,
        },
        "error": error,
        "next_hint": str(next_hint or "")[:300],
        "failure": dict(failure) if failure else None,
    }
