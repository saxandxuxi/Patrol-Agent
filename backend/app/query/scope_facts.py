from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

TIME_FIELDS = frozenset({"collection_time", "create_time"})
PILE_FIELDS = frozenset({"pile_number", "pile_distance"})
INSIDE = "inside"
OUTSIDE = "outside"


@dataclass(frozen=True)
class ScopeFacts:
    verdict: str = ""
    detail: str = ""


def empty_scope_facts(spec: Any) -> ScopeFacts:
    """查询返回 0 行时，这个过滤范围属于哪种情况。

    - `inside`：台账在该范围本来就有数据，这次只是窗口内没有记录（0 条是事实）。
    - `outside`：请求范围超出这份台账的真实数据覆盖（必须如实说明，不能当成 0）。
    - `""`：拿不到事实，按原来的保守处理。
    """

    checks = [
        facts
        for facts in (_time_scope_facts(spec), _pile_scope_facts(spec))
        if facts.verdict
    ]
    if not checks:
        return ScopeFacts()
    for facts in checks:
        if facts.verdict == OUTSIDE:
            return facts
    if all(facts.verdict == INSIDE for facts in checks):
        return ScopeFacts(
            verdict=INSIDE,
            detail="；".join(facts.detail for facts in checks),
        )
    return ScopeFacts()


def _time_scope_facts(spec: Any) -> ScopeFacts:
    window = _time_window(spec)
    if window is None:
        return ScopeFacts()
    try:
        from ..services import calibrate

        route_id = _route_id(spec)
        if route_id:
            facts = calibrate.route_facts(route_id)
            count = facts.get("data_count")
            earliest = facts.get("data_min_time")
            latest = facts.get("data_max_time")
            label = str(facts.get("route") or route_id)
        else:
            facts = calibrate.global_data_range()
            count = facts.get("cnt")
            earliest = facts.get("min_time")
            latest = facts.get("max_time")
            label = "全库病害台账"
    except Exception:  # noqa: BLE001
        return ScopeFacts()
    return _verdict(window, earliest, latest, count, label)


def _pile_scope_facts(spec: Any) -> ScopeFacts:
    """桩号区间同理：问的区间有没有落在该路线的病害数据桩号范围内。"""

    window = _pile_window(spec)
    if window is None:
        return ScopeFacts()
    try:
        from ..services import calibrate

        route_id = _route_id(spec)
        if route_id:
            facts = calibrate.route_facts(route_id)
            low = facts.get("data_min_pile")
            high = facts.get("data_max_pile")
            count = facts.get("data_count")
            label = str(facts.get("route") or route_id)
        else:
            facts = calibrate.global_data_range()
            low = facts.get("min_pile")
            high = facts.get("max_pile")
            count = facts.get("cnt")
            label = "全库病害台账"
    except Exception:  # noqa: BLE001
        return ScopeFacts()
    return _pile_verdict(window, low, high, count, label)


def _route_id(spec: Any) -> str:
    for item in spec.filters:
        field = str(item.field).rsplit(".", 1)[-1]
        if field != "route_name":
            continue
        value = item.storage_value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _time_window(spec: Any) -> tuple[datetime, datetime] | None:
    start: datetime | None = None
    end: datetime | None = None
    for item in spec.filters:
        field = str(item.field).rsplit(".", 1)[-1]
        if field not in TIME_FIELDS:
            continue
        op = str(item.op)
        value = item.storage_value
        if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
            start = _parse(value[0]) or start
            end = _parse(value[1]) or end
            continue
        moment = _parse(value)
        if moment is None:
            continue
        if op in {">=", ">"}:
            start = moment
        elif op in {"<", "<="}:
            end = moment
    if start is None or end is None or end <= start:
        return None
    return start, end


def _pile_window(spec: Any) -> tuple[int, int] | None:
    start: int | None = None
    end: int | None = None
    for item in spec.filters:
        field = str(item.field).rsplit(".", 1)[-1]
        if field not in PILE_FIELDS:
            continue
        op = str(item.op)
        value = item.storage_value
        if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
            pair = (_pile_number(value[0]), _pile_number(value[1]))
            if pair[0] is not None:
                start = pair[0]
            if pair[1] is not None:
                end = pair[1]
            continue
        number = _pile_number(value)
        if number is None:
            continue
        if op in {">=", ">"}:
            start = number
        elif op in {"<", "<="}:
            end = number
    if start is None or end is None or end <= start:
        return None
    return start, end


def _pile_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    return int(text)


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    head, _, _ = text.partition(".")
    candidate = head[:19]
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, pattern)
        except ValueError:
            continue
    return None


def _covered(
    window: tuple[datetime, datetime],
    min_time: Any,
    max_time: Any,
    count: Any,
) -> bool:
    earliest = _parse(min_time)
    latest = _parse(max_time)
    if earliest is None or latest is None:
        return False
    start, end = window
    return start <= latest and end >= earliest


def _pile_verdict(
    window: tuple[int, int],
    min_pile: Any,
    max_pile: Any,
    count: Any,
    label: str,
) -> ScopeFacts:
    try:
        total = int(count or 0)
        low = int(min_pile)
        high = int(max_pile)
    except (TypeError, ValueError):
        return ScopeFacts()
    if total <= 0:
        return ScopeFacts(
            verdict=OUTSIDE,
            detail=f"{label}当前没有任何病害记录",
        )
    covered = (
        f"{label}现有病害记录的桩号在 "
        f"{_format_pile(low)}~{_format_pile(high)}"
    )
    start, end = window
    if start <= high and end >= low:
        return ScopeFacts(verdict=INSIDE, detail=covered)
    return ScopeFacts(verdict=OUTSIDE, detail=covered)


def _format_pile(meters: int) -> str:
    return f"K{meters // 1000}+{meters % 1000:03d}"


def _verdict(
    window: tuple[datetime, datetime],
    min_time: Any,
    max_time: Any,
    count: Any,
    label: str,
) -> ScopeFacts:
    try:
        total = int(count or 0)
    except (TypeError, ValueError):
        return ScopeFacts()
    earliest = _parse(min_time)
    latest = _parse(max_time)
    if total <= 0 or earliest is None or latest is None:
        return ScopeFacts(
            verdict=OUTSIDE,
            detail=f"{label}当前没有任何病害记录",
        )
    covered = (
        f"{label}现有病害记录的采集时间在 "
        f"{earliest:%Y-%m-%d %H:%M} 至 {latest:%Y-%m-%d %H:%M}"
    )
    if _covered(window, earliest, latest, total):
        return ScopeFacts(verdict=INSIDE, detail=covered)
    return ScopeFacts(verdict=OUTSIDE, detail=covered)
