from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..services import dictionary
from .contracts import QuerySpec


class UnsupportedQuerySpec(ValueError):
    """The structured spec cannot be compiled safely without guessing."""


@dataclass
class _SqlParts:
    selects: list[str]
    joins: list[str]
    where: list[str]
    group_by: list[str]
    order_by: list[str]
    metric_alias: str


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _numeric_literal(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _literal(value)
    return str(int(number)) if number.is_integer() else str(number)


_FIELD_ALIASES = {
    "road_level": "route_level",
    "collection_month": "month",
    "collection_time_month": "month",
    "create_time_month": "month",
    "collection_day": "day",
    "collection_time_day": "day",
    "create_time_day": "day",
    "device_id": "equipment_id",
    "device": "equipment_id",
    "equipment": "equipment_id",
    "pile_number_segment": "pile_segment_500m",
    "pile_number_segment_500m": "pile_segment_500m",
    "pile_segment": "pile_segment_500m",
    "pile_number_segment_1000m": "pile_segment_1000m",
    "kilometer_segment": "pile_segment_1000m",
}


def _base_field(field: str) -> str:
    name = str(field).rsplit(".", 1)[-1]
    return _FIELD_ALIASES.get(name, name)


def _pile_segment_expression(meters: int) -> str:
    bucket = f"FLOOR(disease.pile_distance / {meters}) * {meters}"
    return (
        f"CONCAT('K', FLOOR({bucket} / 1000), '+', "
        f"LPAD(MOD({bucket}, 1000), 3, '0'), '~K', "
        f"FLOOR(({bucket} + {meters}) / 1000), '+', "
        f"LPAD(MOD({bucket} + {meters}, 1000), 3, '0'))"
    )


def _pile_meter(value: Any) -> int:
    text = str(value or "").strip().upper().replace("K", "")
    if "+" not in text:
        raise UnsupportedQuerySpec(f"无法解析桩号：{value}")
    kilometer, meter = text.split("+", 1)
    try:
        return int(kilometer) * 1000 + int(meter)
    except ValueError as exc:
        raise UnsupportedQuerySpec(f"无法解析桩号：{value}") from exc


def _disease_filter(
    field: str,
    op: str,
    value: Any,
    joins: set[str],
    where: list[str],
) -> None:
    normalized = _base_field(field)
    if normalized in {"route_name", "routes_id"}:
        if normalized == "route_name":
            if op == "in" or isinstance(value, (list, tuple)):
                values = list(value) if isinstance(value, (list, tuple)) else [value]
                where.append(
                    "disease.route_name IN ("
                    + ", ".join(_literal(item) for item in values)
                    + ")"
                )
            else:
                joins.add("route")
                where.append(f"route.id = {_literal(value)}")
        else:
            if op == "in" or isinstance(value, (list, tuple)):
                values = list(value) if isinstance(value, (list, tuple)) else [value]
                where.append(
                    "disease.routes_id IN ("
                    + ", ".join(_literal(item) for item in values)
                    + ")"
                )
            else:
                where.append(f"disease.routes_id = {_literal(value)}")
        return
    expressions = {
        "problem_type_name": "disease.problem_type_name",
        "up_or_down": "disease.up_or_down",
        "road_surface_type": "disease.road_surface_type",
        "disease_length": "disease.effective_disease_length",
        "effective_disease_length": "disease.effective_disease_length",
        "area": "disease.area",
        "collection_time": "disease.collection_time",
        "create_time": "disease.collection_time",
        "problem_base_id": "disease.problem_base_id",
        "patrol_id": "disease.patrol_id",
        "confidence": "disease.confidence",
        "device_code": "disease.device_code",
        "lane_index": "disease.lane_index",
        "unit_id": "disease.unit_id",
        "patrol_number": "disease.patrol_number",
        "problem_number": "disease.problem_number",
        "task_number": "patrol.task_number",
        "order_number": "work_order.order_number",
    }
    if normalized == "pile_number":
        distance = _pile_meter(value)
        where.append(f"disease.pile_distance {op} {distance}")
        return
    expression = expressions.get(normalized)
    if not expression:
        raise UnsupportedQuerySpec(f"病害查询不支持过滤字段：{field}")
    if normalized == "task_number":
        joins.add("disease_patrol")
    elif normalized == "order_number":
        joins.add("work_order")
    if op == "between" and normalized in {"collection_time", "create_time"}:
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if len(values) != 2:
            raise UnsupportedQuerySpec("时间区间必须提供两个边界值")
        where.append(f"{expression} >= {_literal(values[0])}")
        where.append(f"{expression} < {_literal(values[1])}")
        return
    if op in {"in", "between"} or isinstance(value, (list, tuple)):
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if op == "between":
            if len(values) != 2:
                raise UnsupportedQuerySpec("between 过滤必须提供两个边界值")
            where.append(
                f"{expression} BETWEEN {_literal(values[0])} AND {_literal(values[1])}"
            )
        else:
            where.append(
                f"{expression} IN ({', '.join(_literal(item) for item in values)})"
            )
        return
    if op in {"is_null", "not_null"}:
        where.append(
            f"{expression} IS {'NULL' if op == 'is_null' else 'NOT NULL'}"
        )
        return
    literal = (
        _numeric_literal(value)
        if normalized in {
            "disease_length",
            "effective_disease_length",
            "area",
            "confidence",
            "lane_index",
            "patrol_id",
        }
        else _literal(value)
    )
    where.append(f"{expression} {op} {literal}")


def _patrol_filter(
    field: str,
    op: str,
    value: Any,
    joins: set[str],
    where: list[str],
) -> None:
    normalized = _base_field(field)
    if normalized in {"route_name", "routes_id"}:
        if normalized == "route_name":
            joins.update({"patrol_route", "route"})
            if op == "in" or isinstance(value, (list, tuple)):
                values = list(value) if isinstance(value, (list, tuple)) else [value]
                where.append(
                    "route.short_name IN ("
                    + ", ".join(_literal(item) for item in values)
                    + ")"
                )
            else:
                where.append(f"route.id = {_literal(value)}")
        else:
            joins.update({"patrol_route", "route"})
            if op == "in" or isinstance(value, (list, tuple)):
                values = list(value) if isinstance(value, (list, tuple)) else [value]
                where.append(
                    "patrol_route.routes_id IN ("
                    + ", ".join(_literal(item) for item in values)
                    + ")"
                )
            else:
                where.append(f"patrol_route.routes_id = {_literal(value)}")
        return
    expressions = {
        "equipment_id": "patrol.equipment_id",
        "device_code": "patrol.equipment_id",
        "create_time": "patrol.create_time",
        "collection_time": "patrol.create_time",
        "patrol_mileage": "patrol.patrol_mileage",
        "patrol_id": "patrol.id",
        "unit_id": "patrol.unit_id",
        "up_or_down": "patrol.up_or_down",
        "task_number": "patrol.task_number",
        "patrol_number": "patrol.patrol_number",
        "patrol_status": "patrol.patrol_status",
    }
    expression = expressions.get(normalized)
    if not expression:
        raise UnsupportedQuerySpec(f"巡检任务查询不支持过滤字段：{field}")
    if op == "between" and normalized in {"create_time", "collection_time"}:
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if len(values) != 2:
            raise UnsupportedQuerySpec("时间区间必须提供两个边界值")
        where.append(f"{expression} >= {_literal(values[0])}")
        where.append(f"{expression} < {_literal(values[1])}")
        return
    if op in {"in", "between"} or isinstance(value, (list, tuple)):
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if op == "between":
            if len(values) != 2:
                raise UnsupportedQuerySpec("between 过滤必须提供两个边界值")
            where.append(
                f"{expression} BETWEEN {_literal(values[0])} AND {_literal(values[1])}"
            )
        else:
            where.append(
                f"{expression} IN ({', '.join(_literal(item) for item in values)})"
            )
        return
    if op in {"is_null", "not_null"}:
        where.append(
            f"{expression} IS {'NULL' if op == 'is_null' else 'NOT NULL'}"
        )
        return
    literal = (
        _numeric_literal(value)
        if normalized == "patrol_mileage"
        else _literal(value)
    )
    where.append(f"{expression} {op} {literal}")


def _disease_dimension(
    field: str,
    view: str = "",
    joins: set[str] | None = None,
) -> tuple[str, str]:
    normalized = _base_field(field)
    if normalized in {"route_name", "routes_id"}:
        return "disease.route_name", "路线名称"
    if normalized == "route_level":
        if joins is None:
            raise UnsupportedQuerySpec("路线等级分组缺少路线关联")
        joins.add("route")
        return (
            "CASE route.level "
            "WHEN '0' THEN '国道' WHEN '1' THEN '省道' "
            "WHEN '2' THEN '县道' WHEN '3' THEN '乡道' "
            "WHEN '4' THEN '村道' "
            "ELSE COALESCE(NULLIF(route.level, ''), '未填写') END",
            "道路等级",
        )
    if normalized == "problem_type_name":
        return "COALESCE(disease.problem_type_name, '未识别')", "病害类型"
    if normalized == "up_or_down":
        return (
            "CASE disease.up_or_down "
            "WHEN '1' THEN '上行' WHEN '2' THEN '下行' ELSE '全部/未知' END",
            "上下行方向",
        )
    if normalized == "road_surface_type":
        return (
            "COALESCE(NULLIF(disease.road_surface_type, ''), '未填写')",
            "路面类型",
        )
    if normalized in {"collection_time_month", "month"}:
        return "DATE_FORMAT(disease.collection_time, '%Y-%m')", "月份"
    if normalized in {"collection_time_day", "day"}:
        return "DATE(disease.collection_time)", "日期"
    if normalized == "patrol_number":
        return "disease.patrol_number", "任务编号"
    if normalized == "collection_time":
        if view == "trend":
            return "DATE_FORMAT(disease.collection_time, '%Y-%m')", "月份"
        return "disease.collection_time", "采集时间"
    if normalized == "pile_number":
        return "disease.pile_number", "桩号"
    if normalized == "pile_segment_500m":
        return _pile_segment_expression(500), "桩号分段"
    if normalized == "pile_segment_1000m":
        return _pile_segment_expression(1000), "桩号分段"
    if normalized == "unit_id":
        return "disease.unit_id", "管养单位"
    if normalized == "lane_index":
        return "disease.lane_index", "车道"
    if normalized == "device_code":
        return "disease.device_code", "巡检设备编号"
    raise UnsupportedQuerySpec(f"病害查询不支持分组字段：{field}")


def _patrol_dimension(field: str, joins: set[str]) -> tuple[str, str]:
    normalized = _base_field(field)
    if normalized == "equipment_id":
        return "patrol.equipment_id", "巡检设备"
    if normalized in {"route_name", "routes_id"}:
        joins.update({"patrol_route", "route"})
        return "route.short_name", "路线名称"
    if normalized in {"month", "create_time_month", "collection_time_month"}:
        return "DATE_FORMAT(patrol.create_time, '%Y-%m')", "月份"
    if normalized in {"day", "create_time_day", "collection_time_day"}:
        return "DATE(patrol.create_time)", "日期"
    if normalized == "create_time":
        return "patrol.create_time", "创建时间"
    if normalized == "patrol_name":
        return "patrol.patrol_name", "巡检任务名称"
    if normalized == "patrol_number":
        return "patrol.patrol_number", "任务编号"
    if normalized == "patrol_status":
        return (
            dictionary.case_expression(
                "patrol.patrol_status",
                dictionary.patrol_status_labels(),
            ),
            "巡检状态",
        )
    if normalized in {"id", "patrol_id"}:
        return "patrol.id", "巡检任务ID"
    if normalized == "unit_id":
        return "patrol.unit_id", "管养单位"
    raise UnsupportedQuerySpec(f"巡检任务查询不支持分组字段：{field}")


def _disease_metric(metric: str, joins: set[str]) -> tuple[str, str]:
    return _registered_metric("disease", metric, joins)


def _patrol_metric(metric: str) -> tuple[str, str]:
    return _registered_metric("patrol", metric, set())


def _registered_metric(
    subject: str,
    metric: str,
    joins: set[str],
) -> tuple[str, str]:
    from app.query.capability_registry import CapabilityRegistry
    from app.query.metric_compiler import compile_registered_metric

    normalized = _base_field(metric)
    normalized = {
        "count_task": "count",
        "task_count": "count",
        "record_count": "count",
        "sum_area": "total_area",
        "avg_area": "average_area",
    }.get(normalized, normalized)
    registry = CapabilityRegistry.load()
    metric_cap = registry.snapshot.metrics.get(subject, {}).get(normalized)
    if metric_cap is None:
        available = registry.snapshot.metrics.get(subject, {})
        preferred = [
            item
            for item in (
                "count",
                "total_area",
                "average_area",
                "sum_effective_disease_length",
                "route_length_km",
            )
            if item in available
        ]
        supported = "、".join(
            preferred
            + [
                item
                for item in sorted(available)
                if item not in preferred
            ][: max(0, 24 - len(preferred))]
        )
        raise UnsupportedQuerySpec(
            f"{subject} 查询不支持指标：{metric}；可用指标：{supported}"
        )
    joins.update(metric_cap.requires_join)
    aliases = {
        subject: "disease" if subject == "disease" else "patrol",
        "route": "route",
    }
    return (
        compile_registered_metric(subject, metric_cap, aliases),
        metric_cap.label,
    )


def _disease_output(field: str) -> tuple[str, str] | None:
    normalized = _base_field(field)
    mapping = {
        "id": ("disease.id", "病害ID"),
        "problem_number": ("disease.problem_number", "病害编号"),
        "problem_name": ("disease.problem_name", "病害名称"),
        "problem_type_name": ("disease.problem_type_name", "病害类型"),
        "area": ("disease.area", "面积"),
        "effective_disease_length": ("disease.effective_disease_length", "长度"),
        "pile_number": ("disease.pile_number", "桩号"),
        "route_name": ("disease.route_name", "路线名称"),
        "collection_time": ("disease.collection_time", "采集时间"),
        "problem_url": ("disease.problem_url", "病害图片"),
        "preview_url": ("disease.preview_url", "预览图片"),
        "up_or_down": ("disease.up_or_down", "上下行"),
        "lane_index": ("disease.lane_index", "车道"),
        "road_surface_type": ("disease.road_surface_type", "路面类型"),
        "unit_id": ("disease.unit_id", "管养单位ID"),
        "device_code": ("disease.device_code", "巡检设备编号"),
    }
    return mapping.get(normalized)


def _patrol_output(field: str) -> tuple[str, str] | None:
    normalized = _base_field(field)
    mapping = {
        "id": ("patrol.id", "巡检任务ID"),
        "patrol_id": ("patrol.id", "巡检任务ID"),
        "patrol_name": ("patrol.patrol_name", "巡检任务名称"),
        "patrol_number": ("patrol.patrol_number", "任务编号"),
        "equipment_id": ("patrol.equipment_id", "巡检设备"),
        "create_time": ("patrol.create_time", "创建时间"),
        "end_date": ("patrol.end_date", "结束时间"),
        "patrol_mileage": ("patrol.patrol_mileage", "巡检里程"),
        "patrol_status": (
            dictionary.case_expression(
                "patrol.patrol_status",
                dictionary.patrol_status_labels(),
            ),
            "巡检状态",
        ),
        "unit_id": ("patrol.unit_id", "管养单位ID"),
    }
    return mapping.get(normalized)


def _join_sql(joins: set[str]) -> list[str]:
    values: list[str] = []
    if "route" in joins:
        if "patrol_route" in joins:
            values.extend(
                [
                    "JOIN patrol_route ON patrol_route.patrol_id = patrol.id",
                    "JOIN route ON patrol_route.routes_id = route.id",
                ]
            )
        else:
            values.append("JOIN route ON disease.routes_id = route.id")
    if "disease_patrol" in joins:
        values.append("JOIN patrol ON patrol.id = disease.patrol_id")
    if "work_order" in joins:
        values.append("JOIN ai_work_order ON ai_work_order.problem_id = disease.id")
    return values


def compile_spec_mdl(spec: QuerySpec) -> str | None:
    from .shape_contracts import normalize_top_view

    spec = normalize_top_view(spec)
    if spec.kind != "mysql" or spec.subject not in {"disease", "patrol"}:
        return None
    from .capabilities import validate_capabilities
    from .capability_registry import CapabilityRegistry
    from .shape_contracts import validate_shape_contract

    validate_capabilities(spec)
    shape_issues = validate_shape_contract(spec, CapabilityRegistry.load())
    if shape_issues:
        raise UnsupportedQuerySpec(
            "；".join(issue.reason for issue in shape_issues)
        )
    joins: set[str] = set()
    where: list[str] = []
    if spec.subject == "disease":
        where.append("disease.del_flag = 0")
        for item in spec.filters:
            _disease_filter(item.field, item.op, item.storage_value, joins, where)
    else:
        where.append("patrol.del_flag = 0")
        for item in spec.filters:
            _patrol_filter(item.field, item.op, item.storage_value, joins, where)

    metric_names = {_base_field(metric) for metric in spec.metrics}
    effective_dimensions = tuple(
        field
        for field in spec.dimensions
        if spec.view != "top_groups" or _base_field(field) not in metric_names
    )
    dimensions: list[tuple[str, str]] = []
    for field in effective_dimensions:
        dimensions.append(
            _disease_dimension(field, spec.view, joins)
            if spec.subject == "disease"
            else _patrol_dimension(field, joins)
        )
    metrics: list[tuple[str, str]] = []
    for metric in spec.metrics:
        metrics.append(
            _disease_metric(metric, joins)
            if spec.subject == "disease"
            else _patrol_metric(metric)
        )

    effective_view = spec.view
    if effective_view in {"list", "detail"} and spec.dimensions:
        if metrics:
            effective_view = "distribution"
        else:
            effective_view = "distinct"

    selects: list[str] = []
    output_mapping = (
        _disease_output
        if spec.subject == "disease"
        else _patrol_output
    )
    if effective_view == "top_records":
        for field in spec.output_projection:
            resolved = output_mapping(field)
            if not resolved:
                raise UnsupportedQuerySpec(
                    f"TopN 明细不支持输出字段：{field}"
                )
            selects.append(f"{resolved[0]} AS {resolved[1]}")
    elif effective_view in {"count", "sum", "avg"}:
        if (
            spec.view == "count"
            and len(metrics) == 1
            and _base_field(spec.metrics[0])
            in {"count", "count_task", "task_count", "record_count"}
        ):
            selects = ["COUNT(*) AS 病害数量" if spec.subject == "disease" else "COUNT(*) AS 数量"]
        else:
            selects = [
                f"{expression} AS {label}"
                for expression, label in metrics
            ]
    elif effective_view in {"distribution", "trend", "top_groups", "distinct"}:
        selects = [
            f"{expression} AS {label}"
            for expression, label in dimensions
        ]
        selects.extend(f"{expression} AS {label}" for expression, label in metrics)
    else:
        output_fields = [
            *spec.output_projection,
            *(
                order.get("field")
                for order in spec.order_by
                if isinstance(order, dict) and order.get("field")
            ),
        ]
        for field in dict.fromkeys(output_fields):
            resolved = output_mapping(field)
            if resolved:
                selects.append(f"{resolved[0]} AS {resolved[1]}")
        if not selects:
            defaults = (
                ("problem_number", "problem_type_name", "area", "pile_number", "route_name", "collection_time", "problem_url")
                if spec.subject == "disease"
                else ("id", "patrol_name", "patrol_number", "patrol_mileage", "create_time")
            )
            for field in defaults:
                resolved = output_mapping(field)
                if resolved:
                    selects.append(f"{resolved[0]} AS {resolved[1]}")

    group_by = [
        expression
        for expression, _label in dimensions
        if effective_view in {"distribution", "trend", "top_groups", "distinct"}
    ]
    order_parts: list[str] = []
    dimension_aliases = {
        _base_field(field): expression
        for field, (expression, _label) in zip(effective_dimensions, dimensions)
    }
    metric_aliases = {
        _base_field(metric): expression
        for metric, (expression, _label) in zip(spec.metrics, metrics)
    }
    metric_labels = {
        _base_field(metric): label
        for metric, (_expression, label) in zip(spec.metrics, metrics)
    }
    for order in spec.order_by:
        if not isinstance(order, dict):
            continue
        field = _base_field(str(order.get("field") or ""))
        expression = dimension_aliases.get(field) or metric_aliases.get(field)
        if not expression and effective_view == "top_records":
            resolved = output_mapping(field)
            expression = resolved[0] if resolved else None
        if not expression and spec.subject == "disease":
            expression = {
                "create_time": "disease.create_time",
                "collection_time": "disease.collection_time",
                "patrol_mileage": None,
            }.get(field)
        if not expression and spec.subject == "patrol":
            expression = {
                "create_time": "patrol.create_time",
                "end_date": "patrol.end_date",
                "patrol_mileage": "patrol.patrol_mileage",
            }.get(field)
        if expression:
            direction = str(order.get("direction") or "asc").upper()
            order_parts.append(f"{expression} {direction}")
    if not order_parts and effective_view in {"distribution", "top_groups"} and metrics:
        alias = (
            metric_labels.get(_base_field(spec.metrics[0]))
            if spec.metrics
            else metrics[0][1]
        ) or metrics[0][1]
        order_parts.append(f"{alias} DESC")
    if effective_view == "trend" and dimensions:
        order_parts = [f"{dimensions[0][1]} ASC"]

    limit = spec.limit
    if limit is None and effective_view in {"list", "detail"}:
        limit = 500
    select_sql = ", ".join(selects)
    from_sql = "FROM disease" if spec.subject == "disease" else "FROM patrol"
    join_sql = " ".join(_join_sql(joins))
    sql = f"SELECT {select_sql} {from_sql}"
    if join_sql:
        sql += f" {join_sql}"
    sql += f" WHERE {' AND '.join(where)}"
    if group_by:
        sql += f" GROUP BY {', '.join(group_by)}"
    if order_parts:
        sql += f" ORDER BY {', '.join(order_parts)}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql
