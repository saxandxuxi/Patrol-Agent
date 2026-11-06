from __future__ import annotations

from datetime import datetime
from typing import Any

from ..services import dictionary, parsers, route_resolver
from ..services.nl2sql import known_problem_types
from .contracts import QuerySpecAmbiguity, QuerySpecError, ValueBinding


_CODE_FIELDS = {
    "up_or_down": "up_or_down",
    "problem_status": "problem_status",
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
    if "近三月" not in question and "近三个月" not in question and "最近三个月" not in question:
        return None
    now = datetime.now()
    return _shift_months(now, -3), now


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
    labels = dictionary.code_labels(kind, "ai_problem", column)
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
        parsed = relative or parsers.parse_time_range(question)
        if parsed:
            start, end = parsed
            start_text = start.strftime("%Y-%m-%d %H:%M:%S")
            end_text = end.strftime("%Y-%m-%d %H:%M:%S")
            if relative or op == "between":
                storage_value: Any = (start_text, end_text)
                display_value = f"{start_text} 至 {end_text}"
            elif op in {">=", ">", "="}:
                storage_value = start_text
                display_value = start_text
            elif op in {"<", "<="}:
                storage_value = end_text
                display_value = end_text
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
