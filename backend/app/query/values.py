from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..services import parsers, route_resolver
from ..semantic_aids import detect_evidence, pile_meter, pile_range
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

    raw_values = (
        list(raw_value)
        if isinstance(raw_value, (list, tuple))
        else [raw_value]
    )
    texts = [str(item or "").strip() for item in raw_values]
    texts = [item for item in texts if item]
    if not texts:
        raise QuerySpecAmbiguity(
            "查询条件缺少可执行的病害类型，请补充具体病害名称。"
        )
    labels: list[str] = []
    families: list[str] = []
    for text in texts:
        resolution = resolve_entity(
            registry,
            "disease_type",
            text,
            kind,
        )
        if resolution.status == "ambiguous":
            choices = "、".join(item.label for item in resolution.values[:8])
            raise QuerySpecAmbiguity(
                f"病害类型“{text}”匹配到多个值：{choices}"
            )
        if resolution.status == "unsupported" or not resolution.values:
            raise QuerySpecError(f"病害类型不在数据库字典中：{text}")
        if resolution.family:
            families.append(resolution.family)
        for item in resolution.values:
            if item.label not in labels:
                labels.append(item.label)
    exact = len(texts) == 1 and labels == texts
    storage_value: Any = labels[0] if exact else labels
    display_text = "、".join(texts)
    return ValueBinding(
        field="problem_type_name",
        op="=",
        storage_value=storage_value,
        display_value=display_text,
        source=_source(display_text, question),
        resolver=(
            "damage_type_dictionary"
            if exact
            else (
                "entity_family:" + ",".join(dict.fromkeys(families))
                if families
                else "damage_type_dictionary_expand"
            )
        ),
        raw_text=display_text,
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
    table, column = _enum_location(registry, subject, field)
    raw_items = (
        list(raw_value)
        if isinstance(raw_value, (list, tuple))
        else [raw_value]
    )
    texts = [str(item or "").strip() for item in raw_items]
    texts = [item for item in texts if item]
    if not texts:
        raise QuerySpecError(f"字段 {table}.{column} 缺少可解析的枚举值")
    resolved_values = []
    for text in texts:
        result = resolve_enum(registry, table, column, text)
        if result.status == "ambiguous":
            choices = "、".join(item.label for item in result.values)
            raise QuerySpecAmbiguity(
                f"字段 {table}.{column} 的值“{text}”存在多个匹配：{choices}"
            )
        if result.status != "resolved" or not result.values:
            raise QuerySpecError(
                f"字段 {table}.{column} 的枚举值不在字典中：{text}"
            )
        resolved_values.append(result.values[0])
    codes = [item.code for item in resolved_values]
    display = "、".join(
        item.display or item.label for item in resolved_values
    )
    explicit = all(item and item in question for item in texts)
    return ValueBinding(
        field=field,
        op="=",
        storage_value=codes if len(codes) > 1 else codes[0],
        display_value=display,
        source="user_explicit" if explicit else "model_inference",
        resolver=f"enum:{table}.{column}",
        raw_text="、".join(texts),
    )


def _enum_location(registry: Any, subject: str, field: str) -> tuple[str, str]:
    """字段的枚举归属按字段目录里的物理列来定。

    有些逻辑字段存在别的表上（行政等级 route_level 在路线台账），
    不能拿主体的基表去找枚举。
    """
    capability = registry.snapshot.fields.get(subject, {}).get(field)
    if (
        capability is not None
        and capability.physical_table
        and capability.physical_column
    ):
        return capability.physical_table, capability.physical_column
    if subject in registry.snapshot.subjects:
        return registry.snapshot.subjects[subject].base_table, field
    for table, column in registry.snapshot.enums:
        if column == field:
            return table, column
    return "", field


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
    if _enum_location(enum_registry, subject, field) in enum_registry.snapshot.enums:
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
        if isinstance(raw_value, (list, tuple)):
            route_values = [
                str(item).strip() for item in raw_value if str(item).strip()
            ]
            resolved_routes = [
                route_resolver._resolve_route(item)
                for item in route_values
            ]
            if not resolved_routes or any(
                not route or not route.get("id")
                for route in resolved_routes
            ):
                raise QuerySpecError(
                    f"路线不在数据库台账中：{'、'.join(route_values)}"
                )
            displays = [
                str(
                    route.get("short_name")
                    or route.get("full_name")
                    or route.get("code")
                    or text
                )
                for route, text in zip(resolved_routes, route_values)
            ]
            joined = "、".join(dict.fromkeys(displays))
            return ValueBinding(
                field=field,
                op="in",
                storage_value=list(dict.fromkeys(displays)),
                display_value=joined,
                source=_source(joined, question),
                resolver="route_dictionary",
                raw_text="、".join(route_values),
            )
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
    if field == "lane_index":
        lane = next(
            (
                item
                for item in detect_evidence(question, kind)
                if item.rule_id == "lane_ordinal"
            ),
            None,
        )
        if lane is not None:
            return ValueBinding(
                field=field,
                op=op,
                storage_value=str(lane.candidate_value),
                display_value=lane.raw_span,
                source="code_rule",
                resolver=f"assist_rule:{lane.rule_id}",
                raw_text=lane.raw_span,
            )
    if field == "pile_number":
        parsed_single = pile_meter(raw_value)
        parsed_range = pile_range(raw_value)
        if parsed_range is None:
            parsed_range = pile_range(question)
        if op == "between" or isinstance(raw_value, (list, tuple)):
            values = (
                list(raw_value)
                if isinstance(raw_value, (list, tuple))
                else [raw_value]
            )
            if len(values) == 1:
                values = [values[0], values[0]]
            parsed_values = [pile_meter(item) for item in values]
            if len(parsed_values) == 2 and all(
                item is not None for item in parsed_values
            ):
                return ValueBinding(
                    field=field,
                    op=op,
                    storage_value=(
                        int(parsed_values[0]),
                        int(parsed_values[1]),
                    ),
                    display_value=str(raw_value),
                    source="code_rule",
                    resolver="assist_rule:pile_number",
                    raw_text=str(raw_value),
                )
        if parsed_range is not None:
            return ValueBinding(
                field=field,
                op="between",
                storage_value=parsed_range,
                display_value=str(raw_value or question),
                source="code_rule",
                resolver="assist_rule:pile_interval",
                raw_text=str(raw_value or question),
            )
        if parsed_single is not None:
            return ValueBinding(
                field=field,
                op=op,
                storage_value=parsed_single,
                display_value=str(raw_value),
                source="code_rule",
                resolver="assist_rule:pile_number",
                raw_text=str(raw_value),
            )
    business_values = (
        registry.snapshot.predicates.get("business_values")
        if registry is not None
        else {}
    ) or {}
    field_rules = (
        business_values.get(field)
        if isinstance(business_values, Mapping)
        else None
    ) or {}
    business_rule = (
        field_rules.get(text)
        if isinstance(field_rules, Mapping)
        else None
    )
    if isinstance(business_rule, Mapping):
        return ValueBinding(
            field=field,
            op=str(business_rule.get("op") or op),
            storage_value=business_rule.get("storage_value"),
            display_value=str(
                business_rule.get("display_value") or text
            ),
            source=str(business_rule.get("source") or "business_rule"),
            resolver=str(
                business_rule.get("resolver")
                or f"business_value:{field}:{text}"
            ),
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
