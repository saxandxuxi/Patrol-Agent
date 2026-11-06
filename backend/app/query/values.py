from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..services import parsers, route_resolver
from .contracts import QuerySpecAmbiguity, QuerySpecError, ValueBinding


_TIME_FIELDS = {
    "collection_time",
    "create_time",
    "end_date",
    "open_date",
    "start_date",
    "update_time",
}


def _source(raw_value: Any, question: str) -> str:
    text = str(raw_value or "").strip()
    return "user_explicit" if text and text in question else "model_inference"


def _shift_months(moment: datetime, months: int) -> datetime:
    month_index = moment.year * 12 + moment.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(moment.day, 28)
    return moment.replace(year=year, month=month, day=day)


def _relative_time_range(question: str) -> tuple[datetime, datetime] | None:
    month_words = {
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
        "十": 10,
    }
    match = re.search(
        r"(?:最近|近)\s*(\d+|[一二两三四五六七八九十])\s*个?月",
        question,
    )
    if not match:
        return None
    raw_months = match.group(1)
    months = int(raw_months) if raw_months.isdigit() else month_words[raw_months]
    now = datetime.now()
    return _shift_months(now, -months), now


def _resolve_damage_type(
    raw_value: Any,
    question: str,
    kind: str,
    registry: Any,
) -> ValueBinding:
    from .capability_registry import resolve_entity

    text = str(raw_value or "").strip()
    if not text:
        raise QuerySpecAmbiguity(
            "查询条件缺少可执行的病害类型，请补充具体病害名称。"
        )
    resolution = resolve_entity(
        registry,
        "disease_type",
        text,
        kind,
    )
    if resolution.status == "ambiguous":
        labels = "、".join(item.label for item in resolution.values[:8])
        raise QuerySpecAmbiguity(
            f"病害类型“{text}”匹配到多个值：{labels}"
        )
    if resolution.status == "unsupported" or not resolution.values:
        raise QuerySpecError(f"病害类型不在数据库字典中：{text}")
    labels = [item.label for item in resolution.values]
    exact = len(labels) == 1 and labels[0] == text
    storage_value: Any = labels[0] if exact else labels
    return ValueBinding(
        field="problem_type_name",
        op="=",
        storage_value=storage_value,
        display_value=text,
        source=_source(text, question),
        resolver=(
            "damage_type_dictionary"
            if exact
            else (
                f"entity_family:{resolution.family}"
                if resolution.family
                else "damage_type_dictionary_expand"
            )
        ),
        raw_text=text,
    )


def _resolve_code(
    field: str,
    raw_value: Any,
    question: str,
    kind: str,
    *,
    subject: str = "",
    registry: Any = None,
) -> ValueBinding:
    from .capability_registry import CapabilityRegistry, resolve_enum

    registry = registry or CapabilityRegistry.load(kind)
    table = ""
    if subject and subject in registry.snapshot.subjects:
        table = registry.snapshot.subjects[subject].base_table
    if not table:
        table = next(
            (
                table_name
                for table_name, column_name in registry.snapshot.enums
                if column_name == field
            ),
            "",
        )
    result = resolve_enum(registry, table, field, raw_value)
    if result.status == "ambiguous":
        choices = "、".join(item.label for item in result.values)
        raise QuerySpecAmbiguity(
            f"字段 {table}.{field} 的值“{raw_value}”存在多个匹配：{choices}"
        )
    if result.status != "resolved" or not result.values:
        raise QuerySpecError(
            f"字段 {table}.{field} 的枚举值不在字典中：{raw_value}"
        )
    resolved = result.values[0]
    text = str(raw_value or "").strip()
    return ValueBinding(
        field=field,
        op="=",
        storage_value=resolved.code,
        display_value=resolved.display,
        source=_source(text, question),
        resolver=f"enum:{table}.{field}",
        raw_text=text,
    )


def resolve_value(
    field: str,
    op: str,
    raw_value: Any,
    question: str,
    kind: str = "mysql",
    *,
    subject: str = "",
    registry: Any = None,
) -> ValueBinding:
    if field == "problem_type_name":
        if registry is None:
            from .capability_registry import CapabilityRegistry

            registry = CapabilityRegistry.load(kind)
        value = _resolve_damage_type(raw_value, question, kind, registry)
        return ValueBinding(**{**value.__dict__, "op": op})
    enum_registry = registry
    if enum_registry is None:
        from .capability_registry import CapabilityRegistry

        enum_registry = CapabilityRegistry.load(kind)
    enum_table = (
        enum_registry.snapshot.subjects[subject].base_table
        if subject in enum_registry.snapshot.subjects
        else ""
    )
    if (enum_table, field) in enum_registry.snapshot.enums:
        value = _resolve_code(
            field,
            raw_value,
            question,
            kind,
            subject=subject,
            registry=enum_registry,
        )
        return ValueBinding(**{**value.__dict__, "op": op})
    text = str(raw_value if raw_value is not None else "").strip()
    if not text:
        raise QuerySpecAmbiguity(
            "查询条件缺少可执行值。请补充具体任务、时间范围或比较批次。"
        )
    if field == "route_name":
        route = route_resolver._resolve_route(text)
        if not route or not route.get("id"):
            raise QuerySpecError(f"路线不在数据库台账中：{text}")
        display = str(
            route.get("short_name")
            or route.get("full_name")
            or route.get("code")
            or text
        )
        return ValueBinding(
            field=field,
            op=op,
            storage_value=str(route["id"]),
            display_value=display,
            source=_source(text, question),
            resolver="route_dictionary",
            raw_text=text,
        )
    if field in _TIME_FIELDS:
        relative = _relative_time_range(question)
        raw_parsed = parsers.parse_time_range(text)
        question_parsed = parsers.parse_time_range(question)
        raw_years = set(re.findall(r"(?<!\d)(\d{4})(?!\d)", text))
        parsed = relative or raw_parsed or question_parsed
        if raw_parsed and not relative and raw_years and not any(
            year in question
            or int(year) in {datetime.now().year, datetime.now().year + 1}
            for year in raw_years
        ):
            raw_year = int(sorted(raw_years)[0])
            current_year = datetime.now().year
            if raw_year != current_year:
                start, end = parsed
                parsed = (
                    start.replace(year=start.year + current_year - raw_year),
                    end.replace(year=end.year + current_year - raw_year),
                )
        if parsed:
            start, end = parsed
            start_text = start.strftime("%Y-%m-%d %H:%M:%S")
            end_text = end.strftime("%Y-%m-%d %H:%M:%S")
            if relative or op == "between":
                storage_value: Any = (start_text, end_text)
                display_value = f"{start_text} 至 {end_text}"
            elif op == "=":
                storage_value = (start_text, end_text)
                display_value = f"{start_text} 至 {end_text}"
                op = "between"
            elif op == ">=":
                storage_value = start_text
                display_value = start_text
            elif op == ">":
                storage_value = end_text
                display_value = end_text
                op = ">="
            elif op == "<":
                boundary_text = start_text if raw_parsed is not None else end_text
                storage_value = boundary_text
                display_value = boundary_text
            elif op == "<=":
                storage_value = end_text
                display_value = end_text
                op = "<"
            else:
                storage_value = text
                display_value = text
            return ValueBinding(
                field=field,
                op="between" if relative else op,
                storage_value=storage_value,
                display_value=display_value,
                source=_source(text, question),
                resolver="time_range",
                raw_text=text,
            )
    return ValueBinding(
        field=field,
        op=op,
        storage_value=raw_value,
        display_value=text,
        source=_source(raw_value, question),
        resolver=f"identity:{field}",
        raw_text=text,
    )
