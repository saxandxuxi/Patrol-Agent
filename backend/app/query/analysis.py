from __future__ import annotations

import json
from typing import Any

from ..services import prompts
from ..services.llm import chat_json, role_model
from .contracts import IntentContract, QueryPlan, QuerySpec, QuerySpecError
from .fields import FieldReachSet, PhysicalSchemaView
from .planner import build_query_plan
from .values import resolve_value


def _text_context(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:8000]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def analyze_single_turn(
    question: str,
    kind: str,
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
) -> QueryPlan:
    original = str(question or "").strip()
    if not original:
        raise QuerySpecError("用户问题为空")
    data = chat_json(
        prompts.messages(
            "query_spec",
            question=original,
            kind=kind,
            schema=_text_context(sorted(schema.columns)),
            reach=_text_context(
                [
                    {
                        "table": item.table,
                        "column": item.column,
                        "matched_text": item.matched_text,
                        "source": item.source,
                        "score": item.score,
                    }
                    for item in reach.hits
                ]
            ),
        ),
        model=role_model("semantic"),
    )
    if not isinstance(data, dict):
        raise QuerySpecError("模型没有返回 QuerySpec JSON")
    contract = IntentContract(
        original_question=original,
        kind=kind,
        intent_family=str(data.get("intent_family") or "").strip(),
        final_goal=str(data.get("final_goal") or original).strip(),
        unknown_terms=_string_tuple(data.get("unknown_terms")),
    )
    specs: list[QuerySpec] = []
    dependencies: dict[str, tuple[str, ...]] = {}
    raw_views = data.get("views")
    if not isinstance(raw_views, list):
        raise QuerySpecError("QuerySpec 缺少 views")
    for index, raw in enumerate(raw_views, 1):
        if not isinstance(raw, dict):
            continue
        spec_id = str(raw.get("id") or f"q{index}").strip()
        filters = []
        for item in raw.get("filters") or []:
            if not isinstance(item, dict):
                raise QuerySpecError("过滤条件必须是对象")
            filters.append(
                resolve_value(
                    str(item.get("field") or ""),
                    str(item.get("op") or "="),
                    item.get("raw_value", item.get("value")),
                    original,
                    kind,
                )
            )
        try:
            limit = int(raw["limit"]) if raw.get("limit") is not None else None
        except (TypeError, ValueError) as exc:
            raise QuerySpecError("limit 必须是整数") from exc
        specs.append(
            QuerySpec(
                id=spec_id,
                kind=kind,
                subject=str(raw.get("subject") or "disease").strip(),
                view=str(raw.get("view") or "").strip(),
                metrics=_string_tuple(raw.get("metrics")),
                dimensions=_string_tuple(raw.get("dimensions")),
                filters=tuple(filters),
                output_projection=_string_tuple(raw.get("output_projection")),
                order_by=tuple(raw.get("order_by") or ()),
                limit=limit,
                required_assertions=_string_tuple(raw.get("required_assertions")),
            )
        )
        dependencies[spec_id] = _string_tuple(raw.get("depends_on"))
    return build_query_plan(contract, tuple(specs), schema, reach, dependencies)
