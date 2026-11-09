from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Iterable

from .contracts import QueryPlan, QuerySpec, ValueBinding

QUARTER_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}
TIME_FIELDS = frozenset({"collection_time", "create_time"})


def _quarter_bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
    start = datetime(year, (quarter - 1) * 3 + 1, 1)
    if quarter == 4:
        return start, datetime(year + 1, 1, 1)
    return start, datetime(year, start.month + 3, 1)


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    if month == 12:
        return start, datetime(year + 1, 1, 1)
    return start, datetime(year, month + 1, 1)


def _week_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = datetime(moment.year, moment.month, moment.day) - timedelta(
        days=moment.weekday()
    )
    return start, start + timedelta(days=7)


def _shifted_quarter(moment: datetime, offset: int) -> tuple[datetime, datetime]:
    index = moment.year * 4 + (moment.month - 1) // 3 + offset
    year, quarter = divmod(index, 4)
    return _quarter_bounds(year, quarter + 1)


def _shifted_month(moment: datetime, offset: int) -> tuple[datetime, datetime]:
    index = moment.year * 12 + moment.month - 1 + offset
    year, month = divmod(index, 12)
    return _month_bounds(year, month + 1)


def _parse_month_count(value: str) -> int | None:
    text = str(value or "").strip()
    if text.isdigit():
        count = int(text)
        return count if count > 0 else None
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        count = tens * 10 + ones
        return count if count > 0 else None
    return digits.get(text)


def _rolling_month_window(
    phrase: str,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    text = "".join(str(phrase or "").split())
    match = re.fullmatch(
        r"(?:最近|近|过去)([0-9一二两三四五六七八九十]+)(?:个)?月",
        text,
    )
    if match is None:
        return None
    count = _parse_month_count(match.group(1))
    if count is None:
        return None
    month_index = now.year * 12 + now.month - 1 - count
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(now.day, monthrange(year, month)[1])
    start = datetime(year, month, day)
    end = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return start, end


def resolve_named_period(
    phrase: str,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """把“本季度/今年/第 N 季度”这类自然周期还原成完整日历区间。"""

    text = "".join(str(phrase or "").split())
    if not text:
        return None
    rolling = _rolling_month_window(text, now)
    if rolling is not None:
        return rolling
    year_match = re.search(r"(\d{4})年", text)
    year = int(year_match.group(1)) if year_match else now.year
    quarter_match = re.search(r"第?([一二三四1234])季度", text)
    if quarter_match:
        return _quarter_bounds(year, QUARTER_NUMBERS[quarter_match.group(1)])
    if re.fullmatch(r"(本|这|当)(个)?(季|季度)", text):
        return _quarter_bounds(now.year, (now.month - 1) // 3 + 1)
    if re.fullmatch(r"上(个)?(季|季度)", text):
        return _shifted_quarter(datetime(now.year, now.month, 1), -1)
    if re.fullmatch(r"下(个)?(季|季度)", text):
        return _shifted_quarter(datetime(now.year, now.month, 1), 1)
    if re.fullmatch(r"(本|这|当)(个)?(年|年度)", text):
        return datetime(year, 1, 1), datetime(year + 1, 1, 1)
    if re.fullmatch(r"今(年|年度)", text):
        return datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1)
    if re.fullmatch(r"去(年|年度)", text):
        return datetime(now.year - 1, 1, 1), datetime(now.year, 1, 1)
    if re.fullmatch(r"(本|这|当)(个)?月", text):
        return _month_bounds(now.year, now.month)
    if re.fullmatch(r"上(个)?月", text):
        return _shifted_month(datetime(now.year, now.month, 1), -1)
    if re.fullmatch(r"下(个)?月", text):
        return _shifted_month(datetime(now.year, now.month, 1), 1)
    if re.fullmatch(r"(本|这|当)(个)?(周|星期)", text):
        return _week_bounds(now)
    if re.fullmatch(r"上(个)?(周|星期)", text):
        return _week_bounds(now - timedelta(days=7))
    if re.fullmatch(r"下(个)?(周|星期)", text):
        return _week_bounds(now + timedelta(days=7))
    return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (list, tuple)):
        return _parse_time(value[-1]) if value else None
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _format(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _is_current_day_boundary(moment: datetime, now: datetime) -> bool:
    """Only restore an upper bound that was clipped at the caller's current day."""

    return moment.date() == now.date()


def _binding_window(
    bindings: Iterable[ValueBinding],
) -> tuple[datetime | None, datetime | None]:
    lower = None
    upper = None
    for binding in bindings:
        if binding.op in {"between", ">=", ">"}:
            raw = (
                binding.storage_value[0]
                if binding.op == "between"
                and isinstance(binding.storage_value, (list, tuple))
                and binding.storage_value
                else binding.storage_value
            )
            parsed = _parse_time(raw)
            if parsed is not None:
                lower = parsed
        if binding.op in {"between", "<=", "<"}:
            raw = (
                binding.storage_value[1]
                if binding.op == "between"
                and isinstance(binding.storage_value, (list, tuple))
                and len(binding.storage_value) == 2
                else binding.storage_value
            )
            parsed = _parse_time(raw)
            if parsed is not None:
                upper = parsed
    return lower, upper


def _normalize_rolling_bindings(
    spec: QuerySpec,
    start: datetime,
    end: datetime,
) -> QuerySpec:
    by_field: dict[str, list[ValueBinding]] = {}
    for binding in spec.filters:
        field = str(binding.field).rsplit(".", 1)[-1]
        if field in TIME_FIELDS:
            by_field.setdefault(field, []).append(binding)
    if not by_field:
        return spec
    filters: list[ValueBinding] = []
    normalized_fields: set[str] = set()
    for binding in spec.filters:
        field = str(binding.field).rsplit(".", 1)[-1]
        group = by_field.get(field)
        if group is None:
            filters.append(binding)
            continue
        if field in normalized_fields:
            continue
        normalized_fields.add(field)
        lower, upper = _binding_window(group)
        related = (
            lower is not None
            and upper is not None
            and abs((lower.date() - start.date()).days) <= 1
            and abs((upper.date() - end.date()).days) <= 1
        )
        if not related:
            filters.extend(group)
            continue
        source = group[0].source
        filters.append(
            replace(
                group[0],
                op="between",
                storage_value=[_format(start), _format(end)],
                display_value=f"{_format(start)} 至 {_format(end)}",
                source=source,
                resolver="named_period",
            )
        )
    return replace(spec, filters=tuple(filters))


def _widen_binding(
    binding: Any,
    start: datetime,
    end: datetime,
    now: datetime,
) -> Any:
    field = str(binding.field).rsplit(".", 1)[-1]
    if field not in TIME_FIELDS:
        return binding
    if binding.op == "between" and isinstance(
        binding.storage_value, (list, tuple)
    ) and len(binding.storage_value) == 2:
        lower = _parse_time(binding.storage_value[0])
        upper = _parse_time(binding.storage_value[1])
        if (
            lower is not None
            and upper is not None
            and start <= lower < end
            and upper < end
            and _is_current_day_boundary(upper, now)
        ):
            return replace(
                binding,
                storage_value=[binding.storage_value[0], _format(end)],
                display_value=f"{binding.storage_value[0]} 至 {_format(end)}",
                resolver="named_period",
            )
        return binding
    if binding.op not in {"<", "<="}:
        return binding
    upper = _parse_time(binding.storage_value)
    if (
        upper is None
        or upper >= end
        or not _is_current_day_boundary(upper, now)
    ):
        return binding
    return replace(
        binding,
        storage_value=_format(end),
        display_value=_format(end),
        resolver="named_period",
    )


def normalize_named_period_window(
    plan: QueryPlan,
    phrases: Iterable[str],
    now: datetime,
) -> QueryPlan:
    """把被截断到“今天”的自然周期上界还原成整个周期的结束时间。"""

    window = None
    rolling = False
    for phrase in phrases:
        window = resolve_named_period(str(phrase), now)
        if window is not None:
            rolling = _rolling_month_window(str(phrase), now) is not None
            break
    if window is None:
        return plan
    start, end = window
    changed = False
    nodes = []
    for node in plan.nodes:
        if node.node_type != "query_data" or not node.spec.filters:
            nodes.append(node)
            continue
        if rolling:
            normalized_spec = _normalize_rolling_bindings(
                node.spec,
                start,
                end,
            )
            if normalized_spec != node.spec:
                changed = True
                nodes.append(replace(node, spec=normalized_spec))
            else:
                nodes.append(node)
            continue
        lower_bounds = [
            parsed
            for parsed in (
                _parse_time(item.storage_value)
                for item in node.spec.filters
                if str(item.field).rsplit(".", 1)[-1] in TIME_FIELDS
                and str(item.op) in {">=", ">"}
            )
            if parsed is not None
        ]
        if not any(moment == start for moment in lower_bounds):
            nodes.append(node)
            continue
        filters = []
        node_changed = False
        for binding in node.spec.filters:
            widened = _widen_binding(binding, start, end, now)
            if widened is not binding:
                node_changed = True
            filters.append(widened)
        if node_changed:
            changed = True
            nodes.append(
                replace(node, spec=replace(node.spec, filters=tuple(filters)))
            )
        else:
            nodes.append(node)
    return replace(plan, nodes=tuple(nodes)) if changed else plan


def _filter_key(binding: ValueBinding) -> tuple[Any, ...]:
    return (
        binding.field,
        binding.op,
        repr(binding.storage_value),
        binding.display_value,
        binding.source,
        binding.resolver,
    )


def dedupe_equivalent_filters(plan: QueryPlan) -> QueryPlan:
    """Remove exact semantic duplicates without merging conflicting filters."""

    changed = False
    nodes = []
    for node in plan.nodes:
        seen: set[tuple[Any, ...]] = set()
        filters = []
        for binding in node.spec.filters:
            key = _filter_key(binding)
            if key in seen:
                changed = True
                continue
            seen.add(key)
            filters.append(binding)
        nodes.append(
            replace(node, spec=replace(node.spec, filters=tuple(filters)))
            if len(filters) != len(node.spec.filters)
            else node
        )
    return replace(plan, nodes=tuple(nodes)) if changed else plan
