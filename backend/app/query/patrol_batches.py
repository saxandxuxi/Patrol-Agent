from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

from .contracts import QueryPlan, QuerySpec, ValueBinding


_TIME_FIELDS = frozenset({"collection_time", "create_time"})
_PATROL_IDENTITY_FIELDS = frozenset({"patrol_number", "task_number"})


class PatrolBatchResolutionError(ValueError):
    failure_code = "patrol_batch_not_resolved"

    def __init__(
        self,
        message: str,
        *,
        plan: QueryPlan | None = None,
        blocked_nodes: Mapping[str, str] | None = None,
        status: str = "ambiguous",
        blocked_node_statuses: Mapping[str, str] | None = None,
        blocked_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.plan = plan
        self.blocked_nodes = dict(blocked_nodes or {})
        self.status = status
        self.blocked_node_statuses = dict(blocked_node_statuses or {})
        self.blocked_evidence = {
            str(node_id): dict(evidence)
            for node_id, evidence in (blocked_evidence or {}).items()
        }


def _base_field(value: Any) -> str:
    return str(value or "").rsplit(".", 1)[-1]


def _quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _route_filter(spec: QuerySpec) -> tuple[str, str]:
    candidates = [
        item
        for item in spec.filters
        if _base_field(item.field) in {"route_name", "routes_id"}
    ]
    if len(candidates) != 1:
        raise PatrolBatchResolutionError(
            "实体集合比较必须先确定唯一路线，但当前查询没有且仅有一个路线条件。"
        )
    binding = candidates[0]
    if isinstance(binding.storage_value, (list, tuple, set)):
        raise PatrolBatchResolutionError(
            "实体集合比较的路线条件没有解析成唯一路线台账 ID。"
        )
    route_id = str(binding.storage_value or "").strip()
    if not route_id:
        raise PatrolBatchResolutionError(
            "实体集合比较的路线条件没有解析成唯一路线台账 ID。"
        )
    return route_id, str(binding.display_value or "").strip()


def _single_day_window(spec: QuerySpec) -> tuple[datetime, datetime]:
    time_filters = [
        item
        for item in spec.filters
        if _base_field(item.field) in _TIME_FIELDS
    ]
    if len(time_filters) == 1 and time_filters[0].op == "between":
        raw = list(time_filters[0].storage_value or ())
        if len(raw) == 2:
            start, end = (_as_datetime(raw[0]), _as_datetime(raw[1]))
        else:
            start = end = None
    else:
        lower = next(
            (item for item in time_filters if item.op == ">="),
            None,
        )
        upper = next(
            (item for item in time_filters if item.op == "<"),
            None,
        )
        start = _as_datetime(lower.storage_value) if lower else None
        end = _as_datetime(upper.storage_value) if upper else None
    if start is None or end is None:
        raise PatrolBatchResolutionError(
            "实体集合比较的每个数据节点必须保留一个明确自然日时间窗口。"
        )
    if start.time() != time.min or end != start + timedelta(days=1):
        raise PatrolBatchResolutionError(
            "实体集合比较只能把完整的单个自然日落实为巡检批次；"
            "当前时间窗口不是完整自然日。"
        )
    return start, end


def _find_batches(
    *,
    kind: str,
    route_id: str,
    start: datetime,
    end: datetime,
    query: Callable[[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    sql = (
        "SELECT DISTINCT p.id AS id, p.task_number AS task_number, "
        "p.patrol_number AS patrol_number, p.create_time AS create_time "
        "FROM ai_maintain_patrol p "
        "JOIN ai_maintain_routes_patrol pr ON pr.patrol_id = p.id "
        "WHERE p.del_flag = 0 "
        f"AND pr.routes_id = {_quote(route_id)} "
        f"AND p.create_time >= {_quote(start.isoformat(sep=' '))} "
        f"AND p.create_time < {_quote(end.isoformat(sep=' '))} "
        "ORDER BY p.create_time, p.id"
    )
    result = query(kind, sql)
    rows = result.get("rows") if isinstance(result, Mapping) else None
    return [dict(row) for row in rows or () if isinstance(row, Mapping)]


def _batch_label(row: Mapping[str, Any]) -> str:
    return str(
        row.get("task_number")
        or row.get("patrol_number")
        or row.get("id")
        or "未命名巡检批次"
    )


def _explicit_patrol_identifiers(spec: QuerySpec) -> list[str]:
    identifiers: list[str] = []
    for binding in spec.filters:
        if (
            _base_field(binding.field) not in _PATROL_IDENTITY_FIELDS
            or binding.source != "user_explicit"
            or binding.op not in {"=", "in"}
        ):
            continue
        values = (
            binding.storage_value
            if isinstance(binding.storage_value, (list, tuple, set))
            else (binding.storage_value,)
        )
        for value in values:
            text = str(value or "").strip()
            if text and text not in identifiers:
                identifiers.append(text)
    return identifiers


def _find_patrol_batches_by_identifier(
    identifiers: list[str],
    query: Callable[[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    quoted = ", ".join(_quote(value) for value in identifiers)
    sql = (
        "SELECT p.id AS id, p.task_number AS task_number, "
        "p.patrol_number AS patrol_number, p.create_time AS create_time "
        "FROM ai_maintain_patrol p "
        "WHERE p.del_flag = 0 "
        f"AND (p.task_number IN ({quoted}) OR p.patrol_number IN ({quoted})) "
        "ORDER BY p.create_time, p.id"
    )
    result = query("mysql", sql)
    rows = result.get("rows") if isinstance(result, Mapping) else None
    return [dict(row) for row in rows or () if isinstance(row, Mapping)]


def _explicit_batch_lookup_evidence(
    *,
    requested: list[str],
    missing: list[str],
    batches: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "lookup_scope": "explicit_patrol_identity_lookup",
        "requested_identifiers": list(requested),
        "missing_identifiers": list(missing),
        "matched_batch_count": len(batches),
        "matched_batches": [
            {
                "id": str(batch.get("id") or ""),
                "task_number": str(batch.get("task_number") or ""),
                "patrol_number": str(batch.get("patrol_number") or ""),
                "create_time": str(batch.get("create_time") or ""),
            }
            for batch in batches
        ],
    }


def validate_explicit_patrol_batches(
    plan: QueryPlan,
    *,
    query: Callable[[str, str], dict[str, Any]] | None = None,
) -> QueryPlan:
    """Validate user-specified patrol identities before scope/window semantics."""

    candidates: list[tuple[str, list[str]]] = []
    for node in plan.nodes:
        if node.node_type != "query_data":
            continue
        identifiers = _explicit_patrol_identifiers(node.spec)
        if identifiers:
            candidates.append((node.id, identifiers))
    if not candidates:
        return plan
    if query is None:
        from ..services.db import db_manager

        query = db_manager.query

    blocked_nodes: dict[str, str] = {}
    blocked_evidence: dict[str, dict[str, Any]] = {}
    cache: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for node_id, identifiers in candidates:
        cache_key = tuple(identifiers)
        batches = cache.get(cache_key)
        if batches is None:
            batches = _find_patrol_batches_by_identifier(identifiers, query)
            cache[cache_key] = batches
        found = {
            str(value or "").strip()
            for batch in batches
            for value in (batch.get("task_number"), batch.get("patrol_number"))
            if str(value or "").strip()
        }
        missing = [value for value in identifiers if value not in found]
        if not missing:
            continue
        missing_text = "、".join(missing)
        blocked_nodes[node_id] = (
            f"指定巡检批次 {missing_text} 在巡检任务台账中不存在；"
            "不能把它当成时间覆盖不足或业务零值。"
        )
        blocked_evidence[node_id] = _explicit_batch_lookup_evidence(
            requested=identifiers,
            missing=missing,
            batches=batches,
        )

    if not blocked_nodes:
        return plan
    raise PatrolBatchResolutionError(
        "；".join(blocked_nodes.values()),
        plan=plan,
        blocked_nodes=blocked_nodes,
        status="unsupported",
        blocked_node_statuses={
            node_id: "unsupported" for node_id in blocked_nodes
        },
        blocked_evidence=blocked_evidence,
    )


def _batch_lookup_evidence(
    *,
    route_id: str,
    route_label: str,
    start: datetime,
    end: datetime,
    batches: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "route_display_name": route_label or route_id,
        "route_id": route_id,
        "requested_start": start.isoformat(sep=" ", timespec="seconds"),
        "requested_end": end.isoformat(sep=" ", timespec="seconds"),
        "lookup_scope": "patrol_route_association_batch_lookup",
        "resolved_batch_count": len(batches),
        "resolved_batches": [
            {
                "id": str(batch.get("id") or ""),
                "task_number": str(batch.get("task_number") or ""),
                "patrol_number": str(batch.get("patrol_number") or ""),
                "create_time": str(batch.get("create_time") or ""),
            }
            for batch in batches
        ],
    }


def describe_batch_lookup_evidence(
    evidence: Mapping[str, Any] | None,
) -> str:
    if not evidence:
        return ""
    if evidence.get("lookup_scope") != "patrol_route_association_batch_lookup":
        return ""
    route = str(
        evidence.get("route_display_name")
        or evidence.get("route_id")
        or "未命名路线"
    )
    route_id = str(evidence.get("route_id") or "").strip()
    start = str(evidence.get("requested_start") or "").strip()
    end = str(evidence.get("requested_end") or "").strip()
    count = evidence.get("resolved_batch_count")
    route_note = f"路线“{route}”"
    if route_id and route_id != route:
        route_note += f"（台账 ID {route_id}）"
    window_note = (
        f"时间 [{start}, {end})"
        if start and end
        else "指定时间范围"
    )
    return (
        f"批次核对证据：{route_note}，{window_note}，"
        "已查询巡检任务与路线关联，"
        f"匹配批次 {count if count is not None else '未知'} 个。"
    )


def _resolved_spec(
    spec: QuerySpec,
    *,
    batch: Mapping[str, Any],
    day: date,
) -> QuerySpec:
    remaining = tuple(
        item
        for item in spec.filters
        if _base_field(item.field) not in _TIME_FIELDS
    )
    binding = ValueBinding(
        field="patrol_id",
        op="=",
        storage_value=str(batch.get("id") or ""),
        display_value=_batch_label(batch),
        source="database_lookup",
        resolver="patrol_batch_resolver",
        raw_text=day.isoformat(),
    )
    return replace(spec, filters=(*remaining, binding))


def resolve_entity_comparison_batches(
    plan: QueryPlan,
    *,
    query: Callable[[str, str], dict[str, Any]] | None = None,
) -> QueryPlan:
    comparison_nodes = [
        node
        for node in plan.nodes
        if node.node_type == "post_calc"
        and "dataset_comparison" in node.spec.required_assertions
    ]
    if not comparison_nodes:
        return plan
    if query is None:
        from ..services.db import db_manager

        query = db_manager.query

    by_id = {node.id: node for node in plan.nodes}
    resolved: dict[str, Any] = {}
    blocked_nodes: dict[str, str] = {}
    blocked_node_statuses: dict[str, str] = {}
    blocked_evidence: dict[str, dict[str, Any]] = {}
    for comparison in comparison_nodes:
        dependency_ids = (
            comparison.spec.comparison_depends_on
            or comparison.depends_on
        )
        for dependency in dependency_ids:
            node = by_id.get(dependency)
            if node is None or node.node_type != "query_data":
                continue
            if dependency in resolved:
                continue
            if any(
                _base_field(binding.to_field) == "patrol_number"
                for binding in node.spec.input_bindings
            ):
                continue
            route_id, route_label = _route_filter(node.spec)
            start, end = _single_day_window(node.spec)
            batches = _find_batches(
                kind=node.spec.kind,
                route_id=route_id,
                start=start,
                end=end,
                query=query,
            )
            evidence = _batch_lookup_evidence(
                route_id=route_id,
                route_label=route_label,
                start=start,
                end=end,
                batches=batches,
            )
            blocked_evidence[dependency] = evidence
            day = start.date()
            if not batches:
                label = route_label or route_id
                blocked_nodes[dependency] = (
                    f"路线“{label}”在 {day.isoformat()} 的现有巡检任务与路线"
                    "关联台账中未核实到对应巡检批次，"
                    "无法把该日期落实为真实巡检实体，也不能把空批次当作 0。"
                )
                blocked_node_statuses[dependency] = "unsupported"
                continue
            if len(batches) > 1:
                label = route_label or route_id
                choices = "、".join(_batch_label(item) for item in batches[:5])
                blocked_nodes[dependency] = (
                    f"路线“{label}”在 {day.isoformat()} 对应多个巡检批次"
                    f"（{choices}），无法唯一判断是哪一次。"
                )
                blocked_node_statuses[dependency] = "ambiguous"
                continue
            resolved[dependency] = replace(
                node,
                spec=_resolved_spec(
                    node.spec,
                    batch=batches[0],
                    day=day,
                ),
            )
    partial_plan = (
        replace(
            plan,
            nodes=tuple(resolved.get(node.id, node) for node in plan.nodes),
        )
        if resolved
        else plan
    )
    if blocked_nodes:
        statuses = set(blocked_node_statuses.values())
        raise PatrolBatchResolutionError(
            "；".join(blocked_nodes.values()),
            plan=partial_plan,
            blocked_nodes=blocked_nodes,
            status=(
                "unsupported"
                if statuses == {"unsupported"}
                else "ambiguous"
            ),
            blocked_node_statuses=blocked_node_statuses,
            blocked_evidence=blocked_evidence,
        )
    return partial_plan
