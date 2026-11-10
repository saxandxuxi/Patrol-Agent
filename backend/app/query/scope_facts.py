from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .pile_segments import is_pile_segment_field

TIME_FIELDS = frozenset({"collection_time", "create_time"})
PILE_FIELDS = frozenset({"pile_number", "pile_distance"})
INSIDE = "inside"
OUTSIDE = "outside"
PARTIAL = "partial"


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
    if any(facts.verdict == PARTIAL for facts in checks):
        return ScopeFacts(
            verdict=PARTIAL,
            detail="；".join(facts.detail for facts in checks),
        )
    if all(facts.verdict == INSIDE for facts in checks):
        return ScopeFacts(
            verdict=INSIDE,
            detail="；".join(facts.detail for facts in checks),
        )
    return ScopeFacts()


def spatial_coverage_facts(spec: Any) -> ScopeFacts:
    """空间结果需要说明所依据的桩号台账实际覆盖范围。"""

    explicit = _pile_scope_facts(spec)
    if explicit.verdict:
        return explicit
    if not _has_pile_output(spec):
        return ScopeFacts()
    try:
        from ..services import calibrate

        route_refs = _route_refs(spec)
        if route_refs:
            checks = []
            for route_ref in route_refs:
                facts = calibrate.route_facts(route_ref)
                checks.append(
                    _catalog_pile_facts(
                        facts,
                        str(facts.get("route") or route_ref),
                    )
                )
            return _combine_route_checks(checks)
        return _catalog_pile_facts(
            calibrate.global_data_range(),
            "全库病害台账",
        )
    except Exception:  # noqa: BLE001
        return ScopeFacts()


def coverage_facts(spec: Any) -> ScopeFacts:
    """非空查询也要同时核对时间和空间覆盖事实。"""

    checks = [
        facts
        for facts in (_time_scope_facts(spec), spatial_coverage_facts(spec))
        if facts.verdict
    ]
    if not checks:
        return ScopeFacts()
    outside = [facts for facts in checks if facts.verdict == OUTSIDE]
    if outside:
        return ScopeFacts(
            verdict=OUTSIDE,
            detail="；".join(facts.detail for facts in outside),
        )
    partial = [facts for facts in checks if facts.verdict == PARTIAL]
    if partial:
        return ScopeFacts(
            verdict=PARTIAL,
            detail="；".join(facts.detail for facts in checks),
        )
    if all(facts.verdict == INSIDE for facts in checks):
        return ScopeFacts(
            verdict=INSIDE,
            detail="；".join(facts.detail for facts in checks),
        )
    return ScopeFacts(
        verdict=checks[0].verdict,
        detail="；".join(facts.detail for facts in checks),
    )


def _time_scope_facts(spec: Any) -> ScopeFacts:
    window = _time_window(spec)
    if window is None:
        return ScopeFacts()
    subject = str(getattr(spec, "subject", "") or "").strip()
    if subject and subject != "disease":
        # 非病害主体不能拿病害台账的时间范围替它背书（巡检台账有自己的数据区间）。
        return _subject_time_facts(spec, subject, window)
    try:
        from ..services import calibrate

        route_refs = _route_refs(spec)
        if route_refs:
            checks = []
            for route_ref in route_refs:
                facts = calibrate.route_facts(route_ref)
                checks.append(
                    _verdict(
                        window,
                        facts.get("data_min_time"),
                        facts.get("data_max_time"),
                        facts.get("data_count"),
                        str(facts.get("route") or route_ref),
                    )
                )
            return _combine_route_checks(checks)
        else:
            facts = calibrate.global_data_range()
            count = facts.get("cnt")
            earliest = facts.get("min_time")
            latest = facts.get("max_time")
            label = "全库病害台账"
    except Exception:  # noqa: BLE001
        return ScopeFacts()
    return _verdict(window, earliest, latest, count, label)


def _subject_time_facts(spec: Any, subject: str, window: tuple[datetime, datetime]) -> ScopeFacts:
    """按主体自己的基表核对时间覆盖：直接读该表的时间列范围。"""

    try:
        from ..services.db import db_manager
        from .capability_registry import CapabilityRegistry

        kind = str(getattr(spec, "kind", "mysql") or "mysql")
        registry = CapabilityRegistry.load(kind)
        capability = registry.snapshot.subjects.get(subject)
        if capability is None or not capability.base_table:
            return ScopeFacts()
        field = next(
            (
                str(item.field).rsplit(".", 1)[-1]
                for item in spec.filters
                if str(item.field).rsplit(".", 1)[-1] in TIME_FIELDS
            ),
            "create_time",
        )
        column_capability = registry.snapshot.fields.get(subject, {}).get(field)
        column = (
            column_capability.physical_column
            if column_capability is not None
            and column_capability.physical_column
            else field
        )
        table = capability.base_table
        for where in ("del_flag = 0", "1 = 1"):
            try:
                rows = db_manager.query(
                    kind,
                    f"SELECT COUNT(*) AS cnt, MIN(`{column}`) AS min_time, "
                    f"MAX(`{column}`) AS max_time FROM `{table}` WHERE {where}",
                )["rows"]
                break
            except Exception:  # noqa: BLE001
                rows = []
        if not rows:
            return ScopeFacts()
    except Exception:  # noqa: BLE001
        return ScopeFacts()
    row = rows[0] if isinstance(rows[0], dict) else {}
    return _verdict(
        window,
        row.get("min_time"),
        row.get("max_time"),
        row.get("cnt"),
        f"{table} 台账",
        noun="巡检记录",
    )


def _pile_scope_facts(spec: Any) -> ScopeFacts:
    """桩号区间同理：问的区间有没有落在该路线的病害数据桩号范围内。"""

    window = _pile_window(spec)
    if window is None:
        return ScopeFacts()
    try:
        from ..services import calibrate

        route_refs = _route_refs(spec)
        if route_refs:
            checks = []
            for route_ref in route_refs:
                facts = calibrate.route_facts(route_ref)
                checks.append(
                    _pile_verdict(
                        window,
                        facts.get("data_min_pile"),
                        facts.get("data_max_pile"),
                        facts.get("data_count"),
                        str(facts.get("route") or route_ref),
                        "该路线",
                    )
                )
            return _combine_route_checks(checks)
        else:
            facts = calibrate.global_data_range()
            low = facts.get("min_pile")
            high = facts.get("max_pile")
            count = facts.get("cnt")
            label = "全库病害台账"
            coverage_scope = "全库病害台账"
    except Exception:  # noqa: BLE001
        return ScopeFacts()
    return _pile_verdict(window, low, high, count, label, coverage_scope)


def _route_refs(spec: Any) -> tuple[str, ...]:
    references: list[str] = []
    for item in spec.filters:
        field = str(item.field).rsplit(".", 1)[-1]
        if field != "route_name":
            continue
        value = item.storage_value
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for candidate in values:
            if isinstance(candidate, str) and candidate.strip():
                references.append(candidate.strip())
    return tuple(dict.fromkeys(references))


def _has_pile_output(spec: Any) -> bool:
    return any(
        str(field).rsplit(".", 1)[-1] in PILE_FIELDS
        or is_pile_segment_field(field)
        for field in (
            *getattr(spec, "dimensions", ()),
            *getattr(spec, "output_projection", ()),
        )
    )


def _combine_route_checks(checks: list[ScopeFacts]) -> ScopeFacts:
    if not checks or any(not facts.verdict for facts in checks):
        return ScopeFacts()
    outside = [facts for facts in checks if facts.verdict == OUTSIDE]
    if outside:
        prefix = (
            "部分路线覆盖不足："
            if len(outside) < len(checks)
            else ""
        )
        return ScopeFacts(
            verdict=OUTSIDE,
            detail=prefix + "；".join(facts.detail for facts in checks),
        )
    partial = [facts for facts in checks if facts.verdict == PARTIAL]
    if partial:
        prefix = (
            "部分路线覆盖不完整："
            if len(partial) < len(checks)
            else ""
        )
        return ScopeFacts(
            verdict=PARTIAL,
            detail=prefix + "；".join(facts.detail for facts in checks),
        )
    if all(facts.verdict == INSIDE for facts in checks):
        return ScopeFacts(
            verdict=INSIDE,
            detail="；".join(facts.detail for facts in checks),
        )
    return ScopeFacts()


def _catalog_pile_facts(facts: dict[str, Any], label: str) -> ScopeFacts:
    try:
        total = int(facts.get("data_count", facts.get("cnt")) or 0)
        low = int(
            facts.get("data_min_pile", facts.get("min_pile"))
        )
        high = int(
            facts.get("data_max_pile", facts.get("max_pile"))
        )
    except (TypeError, ValueError):
        return ScopeFacts()
    if total <= 0:
        return ScopeFacts(
            verdict=OUTSIDE,
            detail=f"{label}当前没有任何病害记录",
        )
    return ScopeFacts(
        verdict=INSIDE,
        detail=(
            f"{label}现有病害记录的桩号在 "
            f"{_format_pile(low)}~{_format_pile(high)}"
        ),
    )


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
    coverage_scope: str,
) -> ScopeFacts:
    if count is None:
        return ScopeFacts()
    try:
        total = int(count)
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
    if start >= low and end <= high:
        return ScopeFacts(verdict=INSIDE, detail=covered)
    if start <= high and end >= low:
        return ScopeFacts(
            verdict=PARTIAL,
            detail=(
                f"查询区间仅部分落在{coverage_scope}的现有病害记录覆盖范围内；"
                f"{covered}。未覆盖部分不能判定为无病害，"
                "当前结果只能代表有记录覆盖的重叠部分"
            ),
        )
    return ScopeFacts(
        verdict=OUTSIDE,
        detail=f"查询区间超出{coverage_scope}桩号覆盖范围；{covered}",
    )


def _format_pile(meters: int) -> str:
    return f"K{meters // 1000}+{meters % 1000:03d}"


def _verdict(
    window: tuple[datetime, datetime],
    min_time: Any,
    max_time: Any,
    count: Any,
    label: str,
    noun: str = "病害记录",
) -> ScopeFacts:
    if count is None:
        return ScopeFacts()
    try:
        total = int(count)
    except (TypeError, ValueError):
        return ScopeFacts()
    earliest = _parse(min_time)
    latest = _parse(max_time)
    if total <= 0 or earliest is None or latest is None:
        return ScopeFacts(
            verdict=OUTSIDE,
            detail=f"{label}当前没有任何{noun}",
        )
    covered = (
        f"{label}现有{noun}的采集时间在 "
        f"{earliest:%Y-%m-%d %H:%M} 至 {latest:%Y-%m-%d %H:%M}"
    )
    start, end = window
    if start >= earliest and end <= latest:
        return ScopeFacts(verdict=INSIDE, detail=covered)
    if start <= latest and end >= earliest:
        return ScopeFacts(
            verdict=PARTIAL,
            detail=f"查询区间仅部分落在{label}覆盖内；{covered}",
        )
    return ScopeFacts(verdict=OUTSIDE, detail=covered)
