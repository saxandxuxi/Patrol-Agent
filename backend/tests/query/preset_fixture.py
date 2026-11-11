from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from app.services import preset_lib


PRESET_FIXTURE = {
    "params": {
        "route_id": {
            "label": "路线",
            "source": "routes_with_data",
            "allow_all": True,
        },
        "damage_type": {
            "label": "病害类型",
            "source": "damage_types",
            "allow_all": True,
        },
        "size_metric": {
            "label": "特征口径",
            "options": [
                {"label": "单处面积", "value": "area"},
                {
                    "label": "有效病害长度",
                    "value": "effective_disease_length",
                },
            ],
        },
        "min_size_value": {
            "label": "特征阈值",
            "type": "numeric",
            "hint": "按所选特征口径筛选严格大于该数值的病害",
        },
        "top_n": {
            "label": "取前几名",
            "source": "top_n",
        },
        "device_code": {
            "label": "设备号",
            "source": "device_codes",
            "allow_all": True,
        },
        "date_from": {"label": "起始日期", "type": "time"},
        "date_to": {"label": "结束日期", "type": "time"},
        "date_granularity": {
            "label": "日期粒度",
            "options": [
                {"label": "按日", "value": "day"},
                {"label": "按周", "value": "week"},
                {"label": "按月", "value": "month"},
                {"label": "按季度", "value": "quarter"},
                {"label": "按年", "value": "year"},
            ],
        },
        "section_id": {
            "label": "路段",
            "source": "sections_with_data",
            "allow_all": True,
        },
    },
    "queries": [
        {
            "id": "patrol_times",
            "title": "巡检次数",
            "question": "指定时间范围内巡检了几次？",
            "params": [
                "route_id",
                "date_granularity",
                "date_from",
                "date_to",
            ],
            "match": {
                "shape": ["count"],
                "subject": ["patrol"],
                "slots": ["route", "time_range"],
                "metrics": ["count"],
            },
            "sql": (
                "SELECT COUNT(DISTINCT t.id) AS 巡检次数 "
                "FROM ai_maintain_patrol t "
                "WHERE t.del_flag = 0 "
                "AND ('{route_id}' = '__ALL__' OR EXISTS ("
                "SELECT 1 FROM ai_maintain_routes_patrol pr "
                "WHERE pr.patrol_id = t.id AND pr.del_flag = 0 "
                "AND pr.routes_id = '{route_id}')) "
                "AND t.create_time >= '{date_from} 00:00:00' "
                "AND t.create_time < DATE_ADD('{date_to}', INTERVAL 1 DAY)"
            ),
        },
        {
            "id": "device_month_records",
            "title": "设备病害数",
            "question": "指定设备在时间范围内检出了多少处病害？",
            "params": [
                "device_code",
                "date_granularity",
                "date_from",
                "date_to",
            ],
            "match": {
                "shape": ["count", "distribution"],
                "subject": ["disease"],
                "slots": ["device_code", "time_range"],
                "metrics": ["count"],
            },
            "sql": (
                "SELECT p.device_code AS 设备号, COUNT(*) AS 检出病害数 "
                "FROM ai_problem p "
                "WHERE p.del_flag = 0 "
                "AND p.device_code = '{device_code}' "
                "AND p.collection_time >= '{date_from} 00:00:00' "
                "AND p.collection_time < DATE_ADD('{date_to}', INTERVAL 1 DAY) "
                "GROUP BY p.device_code"
            ),
        },
        {
            "id": "route_month_trend",
            "title": "路线病害趋势",
            "question": "路线病害按时间粒度的变化趋势",
            "params": ["route_id", "date_granularity"],
            "match": {
                "shape": ["trend", "distribution"],
                "subject": ["disease"],
                "group_by": ["month"],
                "slots": ["route"],
                "metrics": ["count", "total_area"],
            },
            "sql": (
                "SELECT DATE_FORMAT(p.collection_time, '%Y-%m') AS 时间, "
                "COUNT(*) AS 处数, ROUND(SUM(p.area), 2) AS 总面积 "
                "FROM ai_problem p "
                "WHERE p.del_flag = 0 AND p.routes_id = '{route_id}' "
                "GROUP BY 时间 ORDER BY 时间"
            ),
        },
        {
            "id": "disease_threshold_distribution",
            "title": "病害阈值分布",
            "question": "超过指定特征的病害分布",
            "params": [
                "route_id",
                "damage_type",
                "size_metric",
                "min_size_value",
            ],
            "sql": (
                "SELECT p.routes_id AS 路线, b.problem_type AS 病害类型, "
                "CASE '{size_metric}' WHEN 'area' THEN p.area "
                "ELSE p.disease_length END AS 特征值 "
                "FROM ai_problem p "
                "JOIN ai_problem_base b ON p.problem_base_id = b.id "
                "WHERE p.del_flag = 0 "
                "AND p.routes_id = '{route_id}' "
                "AND b.problem_type = '{damage_type}' "
                "AND (CASE '{size_metric}' WHEN 'area' THEN p.area "
                "ELSE p.disease_length END) > {min_size_value}"
            ),
        },
        {
            "id": "disease_threshold_records",
            "title": "病害阈值明细",
            "question": "超过指定特征的病害明细",
            "params": [
                "route_id",
                "damage_type",
                "size_metric",
                "min_size_value",
            ],
            "sql": (
                "SELECT p.problem_number AS 病害编号 "
                "FROM ai_problem p "
                "JOIN ai_problem_base b ON p.problem_base_id = b.id "
                "WHERE p.del_flag = 0 "
                "AND p.routes_id = '{route_id}' "
                "AND b.problem_type = '{damage_type}' "
                "AND (CASE '{size_metric}' WHEN 'area' THEN p.area "
                "ELSE p.disease_length END) > {min_size_value} "
                "LIMIT 500"
            ),
        },
        {
            "id": "disease_type_top_by_scope",
            "title": "病害类型 TopN",
            "question": "区域、路线或路段的病害类型前几名",
            "params": ["route_id", "section_id", "top_n"],
            "sql": (
                "SELECT b.problem_type AS 病害类型, COUNT(*) AS 处数 "
                "FROM ai_problem p "
                "JOIN ai_problem_base b ON p.problem_base_id = b.id "
                "WHERE p.del_flag = 0 "
                "AND p.routes_id = '{route_id}' "
                "AND p.sections_id = '{section_id}' "
                "GROUP BY b.problem_type "
                "ORDER BY 处数 DESC LIMIT {top_n}"
            ),
        },
    ],
}


class PresetFixtureTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(
            preset_lib,
            "load",
            side_effect=lambda force=False: deepcopy(PRESET_FIXTURE),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
