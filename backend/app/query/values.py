from __future__ import annotations

from typing import Any

from ..services import dictionary
from ..services.nl2sql import known_problem_types
from .contracts import QuerySpecError, ValueBinding


_CODE_FIELDS = {
    "up_or_down": "up_or_down",
    "problem_status": "problem_status",
    "problem_source": "problem_source",
    "collect": "collect",
    "track_problem": "track_problem",
    "mission_flag": "mission_flag",
    "has_image": "has_image",
}


def _source(raw_value: Any, question: str) -> str:
    text = str(raw_value or "").strip()
    return "user_explicit" if text and text in question else "model_inference"


def _resolve_damage_type(raw_value: Any, question: str, kind: str) -> ValueBinding:
    text = str(raw_value or "").strip()
    if not text:
        raise QuerySpecError("病害类型为空")
    known = [str(item) for item in known_problem_types(kind) if str(item).strip()]
    if text not in known:
        raise QuerySpecError(f"病害类型不在数据库字典中：{text}")
    return ValueBinding(
        field="problem_type_name",
        op="=",
        storage_value=text,
        display_value=text,
        source=_source(text, question),
        resolver="damage_type_dictionary",
        raw_text=text,
    )


def _resolve_code(field: str, raw_value: Any, question: str, kind: str) -> ValueBinding:
    text = str(raw_value or "").strip()
    if not text:
        raise QuerySpecError(f"{field} 的值为空")
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
        raise QuerySpecError(f"{field} 的值为空")
    return ValueBinding(
        field=field,
        op=op,
        storage_value=raw_value,
        display_value=text,
        source=_source(raw_value, question),
        resolver=f"identity:{field}",
        raw_text=text,
    )
