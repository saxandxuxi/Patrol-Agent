from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..services import dictionary, parsers, route_resolver
from ..services.nl2sql import known_problem_types
from .contracts import QuerySpecAmbiguity, QuerySpecError, ValueBinding


_CODE_FIELDS = {
    "up_or_down": "up_or_down",
    "problem_status": "problem_status",
    "patrol_status": "patrol_status",
    "problem_source": "problem_source",
    "collect": "collect",
    "track_problem": "track_problem",
    "mission_flag": "mission_flag",
    "has_image": "has_image",
}
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


def _resolve_damage_type(raw_value: Any, question: str, kind: str) -> ValueBinding:
    text = str(raw_value or "").strip()
    if not text:
        raise QuerySpecAmbiguity(
            "查询条件缺少可执行的病害类型，请补充具体病害名称。"
        )
    known = [str(item) for item in known_problem_types(kind) if str(item).strip()]
    matches = [
        value
        for value in known
        if value == text or text in value or value in text
    ]
    if not matches:
        raise QuerySpecError(f"病害类型不在数据库字典中：{text}")
    exact = len(matches) == 1 and matches[0] == text
    storage_value: Any = matches[0] if exact else matches
    return ValueBinding(
        field="problem_type_name",
        op="=",
        storage_value=storage_value,
        display_value=text,
        source=_source(text, question),
        resolver=(
            "damage_type_dictionary"
            if exact
            else "damage_type_dictionary_expand"
        ),
        raw_text=text,
    )


def _resolve_code(field: str, raw_value: Any, question: str, kind: str) -> ValueBinding:
    text = str(raw_value or "").strip()
    if not text:
        raise QuerySpecAmbiguity(
            "查询条件缺少可执行的枚举值，请补充具体取值。"
        )
    column = _CODE_FIELDS[field]
    table = (
        "ai_maintain_patrol"
        if field == "patrol_status"
        else "ai_problem"
    )
    labels = dictionary.code_labels(kind, table, column)
    reverse = {str(label).strip(): str(code) for code, label in labels.items()}
    storage = reverse.get(text, text)
    return ValueBinding(
        field=field,
        op="=",
        storage_value=storage,
        display_value=str(labels.get(storage) or text),
        source=_source(text, question),
        resolver=f"enum:{field}",
        raw_text=text,
    )


def resolve_value(
    field: str,
    op: str,
    raw_value: Any,
    question: str,
    kind: str = "mysql",
) -> ValueBinding:
    if field == "problem_type_name":
        value = _resolve_damage_type(raw_value, question, kind)
        return ValueBinding(**{**value.__dict__, "op": op})
    if field in _CODE_FIELDS:
        value = _resolve_code(field, raw_value, question, kind)
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
