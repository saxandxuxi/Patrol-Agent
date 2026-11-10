from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..services import dictionary
from ..semantic_aids import pile_meter as parse_pile_meter
from .contracts import QuerySpec
from .pile_segments import (
    parse_pile_segment_meters,
    pile_segment_expression,
)


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
    "damage_severity": "damage_type",
    "severity": "damage_type",
    "pile_number_segment": "pile_segment_500m",
    "pile_number_segment_500m": "pile_segment_500m",
    "pile_segment": "pile_segment_500m",
    "pile_number_segment_1000m": "pile_segment_1000m",
    "kilometer_segment": "pile_segment_1000m",
}


def _base_field(field: str) -> str:
    name = str(field).rsplit(".", 1)[-1]
    return _FIELD_ALIASES.get(name, name)


def _registered_field(
    subject: str,
    field: str,
    registry: Any | None,
    joins: set[str] | None,
    allowed_roles: tuple[str, ...] = (),
) -> Any | None:
    if registry is None:
        return None
    capability = registry.snapshot.fields.get(subject, {}).get(
        _base_field(field)
    )
    if capability is None or capability.status != "available":
        return None
    if allowed_roles and not set(capability.roles).intersection(allowed_roles):
        return None
    if joins is not None:
        joins.update(capability.requires_join)
    return capability


def _enum_display_output(
    subject: str,
    field: str,
    resolved: tuple[str, str] | None,
    registry: Any | None,
) -> tuple[str, str] | None:
    if resolved is None or registry is None:
        return resolved
    expression, label = resolved
    if expression.lstrip().upper().startswith("CASE "):
        return resolved
    capability = registry.snapshot.fields.get(subject, {}).get(
        _base_field(field)
    )
    if capability is None:
        return resolved
    if expression.strip().rsplit(".", 1)[-1] != capability.physical_column:
        return resolved
    enum = registry.snapshot.enums.get(
        (capability.physical_table, capability.physical_column)
    )
    if enum is None or not enum.values:
        return resolved
    labels = {
        str(code): str(item.display or item.label)
        for code, item in enum.values.items()
    }
    return dictionary.case_expression(expression, labels), label


def _append_filter_expression(
    expression: str,
    op: str,
    value: Any,
    data_type: str,
    where: list[str],
) -> None:
    operator = str(op).lower()
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if operator == "between":
        if len(values) != 2:
            raise UnsupportedQuerySpec("between 过滤必须提供两个边界值")
        where.append(
            f"{expression} BETWEEN {_literal(values[0])} "
            f"AND {_literal(values[1])}"
        )
        return
    if operator in {"in", "="} and len(values) > 1:
        where.append(
            f"{expression} IN ("
            + ", ".join(_literal(item) for item in values)
            + ")"
        )
        return
    if operator in {"is_null", "not_null"}:
        where.append(
            f"{expression} IS {'NULL' if operator == 'is_null' else 'NOT NULL'}"
        )
        return
    numeric = any(
        token in str(data_type).lower()
        for token in ("int", "decimal", "double", "float", "numeric", "number")
    )
    literal = _numeric_literal(value) if numeric else _literal(value)
    where.append(f"{expression} {op} {literal}")


def _pile_meter(value: Any) -> int:
    parsed = parse_pile_meter(value)
    if parsed is None:
        raise UnsupportedQuerySpec(f"无法解析桩号：{value}")
    return parsed


def _disease_filter(
    field: str,
    op: str,
    value: Any,
    joins: set[str],
    where: list[str],
    registry: Any | None = None,
) -> None:
    normalized = _base_field(field)
    if normalized == "tech_grade":
        # 公路技术等级在路段台账上（section.tech_grade），按病害的路段关联。
        joins.add("section")
        labels = {
            "0": "高速公路",
            "1": "一级公路",
            "2": "二级公路",
            "3": "三级公路",
            "4": "四级公路",
            "5": "等外公路",
        }
        texts = [
            str(item)
            for item in (value if isinstance(value, (list, tuple)) else [value])
        ]
        labels_used = [labels.get(item, item) for item in texts]
        expression = (
            "CASE section.tech_grade "
            "WHEN '0' THEN '高速公路' WHEN '1' THEN '一级公路' "
            "WHEN '2' THEN '二级公路' WHEN '3' THEN '三级公路' "
            "WHEN '4' THEN '四级公路' WHEN '5' THEN '等外公路' "
            "ELSE COALESCE(NULLIF(section.tech_grade, ''), '未填写') END"
        )
        if op in {"in", "="} and len(labels_used) > 1:
            where.append(
                expression
                + " IN ("
                + ", ".join(_literal(item) for item in labels_used)
                + ")"
            )
            return
        where.append(f"{expression} {op} {_literal(labels_used[0])}")
        return
    if normalized == "route_level":
        # 行政等级存在路线台账上（route.level），普通病害查询要显式关联；
        # 值可能已经过枚举解析（'1'）也可能还是中文标签（'省道'），两种都认。
        joins.add("route")
        labels = {
            "0": "国道",
            "1": "省道",
            "2": "县道",
            "3": "乡道",
            "4": "村道",
        }
        texts = [
            str(item)
            for item in (value if isinstance(value, (list, tuple)) else [value])
        ]
        labels_used = [labels.get(item, item) for item in texts]
        expression = (
            "CASE route.level "
            "WHEN '0' THEN '国道' WHEN '1' THEN '省道' "
            "WHEN '2' THEN '县道' WHEN '3' THEN '乡道' "
            "WHEN '4' THEN '村道' "
            "ELSE COALESCE(NULLIF(route.level, ''), '未填写') END"
        )
        if op in {"in", "="} and len(labels_used) > 1:
            where.append(
                expression
                + " IN ("
                + ", ".join(_literal(item) for item in labels_used)
                + ")"
            )
            return
        where.append(f"{expression} {op} {_literal(labels_used[0])}")
        return
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
        "history_damage_interval": "disease.history_damage_interval",
        "device_code": "disease.device_code",
        "lane_index": "disease.lane_index",
        "unit_id": "disease.unit_id",
        "patrol_number": "disease.patrol_number",
        "problem_number": "disease.problem_number",
        "task_number": "patrol.task_number",
        "order_number": "work_order.order_number",
    }
    if normalized == "pile_number":
        # 起止桩号区间（"K120 到 K125"）是范围过滤：两个边界都要落到
        # pile_distance 上。此前只处理单点，区间会当成一个值去解析桩号，
        # 直接报“无法解析桩号”。
        if op in {"in", "between"} or isinstance(value, (list, tuple)):
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            if op == "between":
                if len(values) != 2:
                    raise UnsupportedQuerySpec("桩号区间必须提供两个边界值")
                start = _pile_meter(values[0])
                end = _pile_meter(values[1])
                where.append(
                    f"disease.pile_distance BETWEEN {start} AND {end}"
                )
                return
            distances = [_pile_meter(item) for item in values]
            where.append(
                "disease.pile_distance IN ("
                + ", ".join(str(item) for item in distances)
                + ")"
            )
            return
        distance = _pile_meter(value)
        where.append(f"disease.pile_distance {op} {distance}")
        return
    if parse_pile_segment_meters(normalized) is not None:
        # 上一节点常把“高发桩号分段”作为依赖值传下来。分段是注册维度
        # （由桩号换算的表达式），不是物理列，按同一表达式过滤即可，
        # 不能凭空报“不支持过滤字段”。
        segment_expression, _label = _disease_dimension(normalized, "", joins)
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if op in {"in", "="} and len(values) > 1:
            where.append(
                segment_expression
                + " IN ("
                + ", ".join(_literal(item) for item in values)
                + ")"
            )
            return
        where.append(f"{segment_expression} {op} {_literal(values[0])}")
        return
    expression = expressions.get(normalized)
    if not expression:
        capability = _registered_field(
            "disease",
            normalized,
            registry,
            joins,
        )
        if capability is not None:
            _append_filter_expression(
                capability.filter_expression or capability.expression,
                op,
                value,
                capability.data_type,
                where,
            )
            return
        raise UnsupportedQuerySpec(
            f"病害查询不支持过滤字段：{field}；"
            "可用过滤字段：route_name、pile_number、"
            + "、".join(sorted(expressions))
        )
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
    registry: Any | None = None,
) -> None:
    normalized = _base_field(field)
    if normalized in {"pile_number", "pile_distance"}:
        joins.add("disease")
        expression = (
            "disease.pile_distance"
            if normalized == "pile_distance"
            else "disease.pile_distance"
        )
        if op in {"in", "between"} or isinstance(value, (list, tuple)):
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            if op == "between":
                if len(values) != 2:
                    raise UnsupportedQuerySpec("桩号区间必须提供两个边界值")
                start = _pile_meter(values[0])
                end = _pile_meter(values[1])
                where.append(f"{expression} BETWEEN {start} AND {end}")
                return
            distances = [_pile_meter(item) if normalized == "pile_number" else int(item) for item in values]
            where.append(
                f"{expression} IN ("
                + ", ".join(str(item) for item in distances)
                + ")"
            )
            return
        distance = _pile_meter(value) if normalized == "pile_number" else int(value)
        where.append(f"{expression} {op} {distance}")
        return
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
        capability = _registered_field(
            "patrol",
            normalized,
            registry,
            joins,
        )
        if capability is not None:
            _append_filter_expression(
                capability.filter_expression or capability.expression,
                op,
                value,
                capability.data_type,
                where,
            )
            return
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
    registry: Any | None = None,
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
    if normalized == "tech_grade":
        if joins is None:
            raise UnsupportedQuerySpec("公路技术等级分组缺少路段台账关联")
        joins.add("section")
        return (
            "CASE section.tech_grade "
            "WHEN '0' THEN '高速公路' WHEN '1' THEN '一级公路' "
            "WHEN '2' THEN '二级公路' WHEN '3' THEN '三级公路' "
            "WHEN '4' THEN '四级公路' WHEN '5' THEN '等外公路' "
            "ELSE COALESCE(NULLIF(section.tech_grade, ''), '未填写') END",
            "公路技术等级",
        )
    if normalized in {"collection_time_month", "month"}:
        return "DATE_FORMAT(disease.collection_time, '%Y-%m')", "月份"
    if normalized == "week":
        return (
            "CONCAT(DATE_FORMAT(disease.collection_time, '%x'), "
            "'-W', DATE_FORMAT(disease.collection_time, '%v'))",
            "周",
        )
    if normalized == "collection_time_year":
        return "DATE_FORMAT(disease.collection_time, '%Y')", "年份"
    if normalized == "collection_time_quarter":
        return (
            "CONCAT(YEAR(disease.collection_time), '-Q', "
            "QUARTER(disease.collection_time))",
            "季度",
        )
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
    segment_meters = parse_pile_segment_meters(normalized)
    if segment_meters is not None:
        return pile_segment_expression(segment_meters), "桩号分段"
    if normalized == "pile_distance":
        raise UnsupportedQuerySpec(
            "不支持按原始桩号数值分组；固定长度分段统计必须使用 "
            "pile_segment_<N>m，例如 pile_segment_2000m 表示每2公里。"
        )
    if normalized == "luge_name":
        raise UnsupportedQuerySpec(
            "luge_name 是里程区段编号（形如 k0+800-k0+899），不支持作为"
            "固定长度分段的聚合维度；分段统计用 pile_segment_<N>m。"
        )
    if normalized == "unit_id":
        return "disease.unit_id", "管养单位"
    if normalized == "lane_index":
        return "disease.lane_index", "车道"
    if normalized == "device_code":
        return "disease.device_code", "巡检设备编号"
    if normalized == "history_damage_interval":
        return (
            "COALESCE(NULLIF(disease.history_damage_interval, ''), '未填写')",
            "损坏程度",
        )
    capability = _registered_field(
        "disease",
        normalized,
        registry,
        joins,
        allowed_roles=("dimension",),
    )
    if capability is not None:
        return capability.expression, capability.label
    raise UnsupportedQuerySpec(f"病害查询不支持分组字段：{field}")


def _patrol_dimension(
    field: str,
    joins: set[str],
    registry: Any | None = None,
) -> tuple[str, str]:
    normalized = _base_field(field)
    if normalized == "equipment_id":
        return "patrol.equipment_id", "巡检设备"
    if normalized == "device_code":
        # 设备编号与过滤条件落同一物理列（patrol.equipment_id），
        # 不能过滤一个列、分组另一个列。
        return "patrol.equipment_id", "设备编号"
    if normalized in {"route_name", "routes_id"}:
        joins.update({"patrol_route", "route"})
        return "route.short_name", "路线名称"
    if normalized in {"month", "create_time_month", "collection_time_month"}:
        return "DATE_FORMAT(patrol.create_time, '%Y-%m')", "月份"
    if normalized in {"day", "create_time_day", "collection_time_day"}:
        return "DATE(patrol.create_time)", "日期"
    if normalized in {"create_time", "collection_time"}:
        return "patrol.create_time", "创建时间"
    if normalized == "patrol_name":
        return "patrol.patrol_name", "巡检任务名称"
    if normalized == "patrol_number":
        return "patrol.patrol_number", "任务编号"
    if normalized == "pile_number":
        joins.add("disease")
        return "disease.pile_number", "桩号"
    segment_meters = parse_pile_segment_meters(normalized)
    if segment_meters is not None:
        joins.add("disease")
        return pile_segment_expression(segment_meters), "桩号分段"
    if normalized == "task_number":
        return "patrol.task_number", "任务编号"
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
    capability = _registered_field(
        "patrol",
        normalized,
        registry,
        joins,
        allowed_roles=("dimension",),
    )
    if capability is not None:
        return capability.expression, capability.label
    raise UnsupportedQuerySpec(f"巡检任务查询不支持分组字段：{field}")


def _disease_metric(metric: str, joins: set[str]) -> tuple[str, str]:
    return _registered_metric("disease", metric, joins)


def _patrol_metric(metric: str, joins: set[str]) -> tuple[str, str]:
    return _registered_metric("patrol", metric, joins)


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


def _disease_output(
    field: str,
    registry: Any | None = None,
    joins: set[str] | None = None,
) -> tuple[str, str] | None:
    normalized = _base_field(field)
    mapping = {
        "id": ("disease.id", "病害ID"),
        "problem_number": ("disease.problem_number", "病害编号"),
        "problem_name": ("disease.problem_name", "病害名称"),
        "problem_type_name": ("disease.problem_type_name", "病害类型"),
        "area": ("disease.area", "面积"),
        "disease_length": (
            "disease.effective_disease_length",
            "实际病害长度",
        ),
        "effective_disease_length": ("disease.effective_disease_length", "长度"),
        "pile_number": ("disease.pile_number", "桩号"),
        "route_name": ("disease.route_name", "路线名称"),
        "collection_time": ("disease.collection_time", "采集时间"),
        "confidence": ("disease.confidence", "AI置信度"),
        "problem_url": ("disease.problem_url", "病害图片"),
        "preview_url": ("disease.preview_url", "预览图片"),
        "up_or_down": ("disease.up_or_down", "上下行"),
        "lane_index": ("disease.lane_index", "车道"),
        "road_surface_type": ("disease.road_surface_type", "路面类型"),
        "unit_id": ("disease.unit_id", "管养单位ID"),
        "device_code": ("disease.device_code", "巡检设备编号"),
        "history_damage_interval": (
            "disease.history_damage_interval",
            "损坏程度",
        ),
    }
    resolved = mapping.get(normalized)
    if resolved is not None:
        return _enum_display_output("disease", normalized, resolved, registry)
    if registry is None:
        return None
    capability = registry.snapshot.fields.get("disease", {}).get(normalized)
    if capability is None or capability.status != "available":
        return None
    if joins is not None:
        joins.update(capability.requires_join)
    return _enum_display_output(
        "disease",
        normalized,
        (capability.expression, capability.label),
        registry,
    )


def _patrol_output(
    field: str,
    registry: Any | None = None,
    joins: set[str] | None = None,
) -> tuple[str, str] | None:
    normalized = _base_field(field)
    mapping = {
        "id": ("patrol.id", "巡检任务ID"),
        "patrol_id": ("patrol.id", "巡检任务ID"),
        "patrol_name": ("patrol.patrol_name", "巡检任务名称"),
        "task_number": ("patrol.task_number", "任务编号"),
        "patrol_number": ("patrol.patrol_number", "任务编号"),
        "pile_number": ("disease.pile_number", "桩号"),
        "equipment_id": ("patrol.equipment_id", "巡检设备"),
        "device_code": ("patrol.equipment_id", "设备编号"),
        "create_time": ("patrol.create_time", "创建时间"),
        "collection_time": ("patrol.create_time", "巡检时间"),
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
    resolved = mapping.get(normalized)
    if resolved is not None:
        return _enum_display_output("patrol", normalized, resolved, registry)
    if registry is None:
        return None
    capability = registry.snapshot.fields.get("patrol", {}).get(normalized)
    if capability is None or capability.status != "available":
        return None
    if joins is not None:
        joins.update(capability.requires_join)
    return _enum_display_output(
        "patrol",
        normalized,
        (capability.expression, capability.label),
        registry,
    )


def _join_sql(
    joins: set[str],
    subject_joins: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    subject_joins = subject_joins or {}

    def keyword(name: str) -> str:
        declaration = subject_joins.get(name) or {}
        return (
            "LEFT JOIN"
            if str(declaration.get("type") or "").lower() == "left"
            else "JOIN"
        )

    values: list[str] = []
    if "route" in joins:
        if "patrol_route" in joins:
            values.extend(
                [
                    f"{keyword('patrol_route')} patrol_route "
                    "ON patrol_route.patrol_id = patrol.id",
                    f"{keyword('route')} route "
                    "ON patrol_route.routes_id = route.id",
                ]
            )
        else:
            values.append(
                f"{keyword('route')} route ON disease.routes_id = route.id"
            )
    if "disease_patrol" in joins:
        values.append("JOIN patrol ON patrol.id = disease.patrol_id")
    if "disease" in joins:
        values.append(
            f"{keyword('disease')} disease ON disease.patrol_id = patrol.id "
            "AND disease.del_flag = 0"
        )
    if "section" in joins:
        values.append(
            "JOIN section ON section.id = disease.sections_id "
            "AND section.del_flag = 0"
        )
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

    registry = CapabilityRegistry.load()
    validate_capabilities(spec, registry)
    shape_issues = validate_shape_contract(spec, registry)
    if shape_issues:
        raise UnsupportedQuerySpec(
            "；".join(issue.reason for issue in shape_issues)
        )
    joins: set[str] = set()
    where: list[str] = []
    if spec.subject == "disease":
        where.append("disease.del_flag = 0")
        for item in spec.filters:
            _disease_filter(
                item.field,
                item.op,
                item.storage_value,
                joins,
                where,
                registry,
            )
    else:
        where.append("patrol.del_flag = 0")
        for item in spec.filters:
            _patrol_filter(
                item.field,
                item.op,
                item.storage_value,
                joins,
                where,
                registry,
            )

    metric_names = {_base_field(metric) for metric in spec.metrics}
    effective_dimensions = tuple(
        field
        for field in spec.dimensions
        if spec.view != "top_groups" or _base_field(field) not in metric_names
    )
    dimensions: list[tuple[str, str]] = []
    for field in effective_dimensions:
        dimensions.append(
            _disease_dimension(field, spec.view, joins, registry)
            if spec.subject == "disease"
            else _patrol_dimension(field, joins, registry)
        )
    metrics: list[tuple[str, str]] = []
    for metric in spec.metrics:
        metrics.append(
            _disease_metric(metric, joins)
            if spec.subject == "disease"
            else _patrol_metric(metric, joins)
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
            resolved = output_mapping(field, registry, joins)
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
            resolved = output_mapping(field, registry, joins)
            if not resolved:
                raise UnsupportedQuerySpec(
                    f"明细查询不支持输出或排序字段：{field}"
                )
            selects.append(f"{resolved[0]} AS {resolved[1]}")
        if not selects:
            defaults = (
                ("problem_number", "problem_type_name", "area", "pile_number", "route_name", "collection_time", "problem_url")
                if spec.subject == "disease"
                else ("id", "patrol_name", "patrol_number", "patrol_mileage", "create_time")
            )
            for field in defaults:
                resolved = output_mapping(field, registry, joins)
                if not resolved:
                    raise UnsupportedQuerySpec(
                        f"明细查询默认输出字段不可用：{field}"
                    )
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
    dimension_labels = {
        _base_field(field): label
        for field, (_expression, label) in zip(effective_dimensions, dimensions)
    }
    metric_aliases = {
        _base_field(metric): expression
        for metric, (expression, _label) in zip(spec.metrics, metrics)
    }
    metric_labels = {
        _base_field(metric): label
        for metric, (_expression, label) in zip(spec.metrics, metrics)
    }
    outer_order_parts: list[str] = []
    for order in spec.order_by:
        if not isinstance(order, dict):
            continue
        field = _base_field(str(order.get("field") or ""))
        expression = dimension_aliases.get(field) or metric_aliases.get(field)
        if not expression:
            resolved = output_mapping(field, registry, joins)
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
                "collection_time": "patrol.create_time",
                "end_date": "patrol.end_date",
                "patrol_mileage": "patrol.patrol_mileage",
            }.get(field)
        if expression:
            direction = str(order.get("direction") or "asc").upper()
            order_parts.append(f"{expression} {direction}")
            label = (
                dimension_labels.get(field)
                or metric_labels.get(field)
            )
            if label:
                outer_order_parts.append(f"`{label}` {direction}")
    if not order_parts and effective_view in {"distribution", "top_groups"} and metrics:
        alias = (
            metric_labels.get(_base_field(spec.metrics[0]))
            if spec.metrics
            else metrics[0][1]
        ) or metrics[0][1]
        order_parts.append(f"{alias} DESC")
        outer_order_parts.append(f"`{alias}` DESC")
    if effective_view == "trend" and dimensions:
        order_parts = [f"{dimensions[0][1]} ASC"]

    limit = spec.limit
    if limit is None and effective_view in {"list", "detail"}:
        limit = 500
    select_sql = ", ".join(selects)
    from_sql = "FROM disease" if spec.subject == "disease" else "FROM patrol"
    subject_capability = registry.snapshot.subjects.get(spec.subject)
    join_sql = " ".join(
        _join_sql(
            joins,
            subject_capability.joins if subject_capability is not None else None,
        )
    )
    if (
        effective_view == "top_groups"
        and "top_per_outer_group" in spec.required_assertions
    ):
        if len(dimensions) < 2:
            raise UnsupportedQuerySpec(
                "top_per_outer_group 至少需要外层分组维度和组内排名维度"
            )
        outer_columns = ", ".join(
            f"`{label}`"
            for _expression, label in (*dimensions, *metrics)
        )
        rank_sql = (
            "ROW_NUMBER() OVER ("
            f"PARTITION BY {dimensions[0][0]} "
            f"ORDER BY {', '.join(order_parts)}"
            ") AS __top_rank"
        )
        inner_sql = (
            f"SELECT {select_sql}, {rank_sql} {from_sql}"
            f" {join_sql} WHERE {' AND '.join(where)}"
            f" GROUP BY {', '.join(group_by)}"
        )
        outer_order = ", ".join(outer_order_parts) or (
            f"`{dimensions[0][1]}` ASC"
        )
        return (
            f"SELECT {outer_columns} FROM ({inner_sql}) AS ranked "
            f"WHERE __top_rank <= {int(limit)} "
            f"ORDER BY {outer_order}"
        )
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
