import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.spec_mdl import UnsupportedQuerySpec, compile_spec_mdl


def binding(field, op, value):
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="user_explicit",
        resolver="test",
        raw_text=str(value),
    )


class SpecMdlTests(unittest.TestCase):
    def test_top_records_compiles_detail_order_and_limit(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="top_records",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=("problem_number", "area"),
                order_by=({"field": "area", "direction": "desc"},),
                limit=5,
                required_assertions=(),
                question="按面积取前5条病害记录",
            )
        )

        self.assertIn("disease.problem_number AS 病害编号", sql)
        self.assertIn("disease.area AS 面积", sql)
        self.assertIn("ORDER BY disease.area DESC", sql)
        self.assertIn("LIMIT 5", sql)
        self.assertNotIn("GROUP BY", sql)
        self.assertNotIn("SELECT FROM", sql)

    def test_top_groups_compiles_group_order_and_limit(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="top_groups",
                metrics=("count",),
                dimensions=("route_name",),
                filters=(),
                output_projection=(),
                order_by=({"field": "count", "direction": "desc"},),
                limit=5,
                required_assertions=(),
                question="按病害数量取前5条路线",
            )
        )

        self.assertIn("COUNT(*) AS 数量", sql)
        self.assertIn("disease.route_name AS 路线名称", sql)
        self.assertIn("GROUP BY disease.route_name", sql)
        self.assertIn("ORDER BY COUNT(*) DESC", sql)
        self.assertIn("LIMIT 5", sql)

    def test_unsupported_metric_error_lists_supported_metrics(self):
        with self.assertRaises(UnsupportedQuerySpec) as caught:
            compile_spec_mdl(
                QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="disease",
                    view="distribution",
                    metrics=("disease_density",),
                    dimensions=("route_name",),
                    filters=(),
                    output_projection=("route_name", "disease_density"),
                    order_by=(),
                    limit=None,
                    required_assertions=(),
                    question="比较路线病害密度",
                )
            )

        self.assertIn("count", str(caught.exception))
        self.assertIn("route_length_km", str(caught.exception))

    def test_disease_distribution_can_group_by_patrol_number(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q2",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("patrol_number",),
                filters=(binding("problem_type_name", "=", "坑槽"),),
                output_projection=("patrol_number", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按巡查批次统计坑槽数量",
            )
        )

        self.assertIn("disease.patrol_number AS 任务编号", sql)
        self.assertIn("GROUP BY disease.patrol_number", sql)

    def test_disease_distribution_can_group_by_device_code(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("device_code",),
                filters=(binding("device_code", "=", "102367594561"),),
                output_projection=("device_code", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="每台巡检设备检出了多少处病害",
            )
        )

        self.assertIn("disease.device_code AS 巡检设备编号", sql)
        self.assertIn("GROUP BY disease.device_code", sql)

    def test_patrol_list_can_output_end_date(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=("patrol_number", "end_date"),
                order_by=({"field": "end_date", "direction": "desc"},),
                limit=5,
                required_assertions=(),
                question="列出最近5次巡查",
            )
        )

        self.assertIn("patrol.end_date AS 结束时间", sql)
        self.assertIn("ORDER BY patrol.end_date DESC", sql)
        self.assertIn("LIMIT 5", sql)

    @patch("app.query.spec_mdl.dictionary.patrol_status_labels")
    def test_patrol_status_dimension_uses_display_labels(self, status_labels):
        status_labels.return_value = {"0": "未开始", "15": "已完成"}

        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("count",),
                dimensions=("patrol_status",),
                filters=(),
                output_projection=("patrol_status", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按巡检状态统计任务数量",
            )
        )

        self.assertIn("CASE patrol.patrol_status", sql)
        self.assertIn("'未开始'", sql)
        self.assertIn("'已完成'", sql)

    def test_count_view_keeps_all_requested_metrics(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="count",
                metrics=("count", "sum_area"),
                dimensions=(),
                filters=(),
                output_projection=("count", "sum_area"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="统计上行病害数量和总面积",
            )
        )

        self.assertIn("COUNT(*) AS", sql)
        self.assertIn("SUM(disease.area)", sql)

    def test_pile_segment_aliases_compile_to_same_bucket(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="top",
                metrics=("count",),
                dimensions=("pile_number_segment_500m",),
                filters=(),
                output_projection=("pile_number_segment_500m", "count"),
                order_by=(),
                limit=10,
                required_assertions=(),
                question="按500米桩号分段统计",
            )
        )

        self.assertIn("FLOOR(disease.pile_distance / 500)", sql)
        self.assertIn("AS 桩号分段", sql)

    def test_disease_day_distribution_uses_calendar_day(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("collection_time_day",),
                filters=(),
                output_projection=("collection_time_day", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按日统计病害数量",
            )
        )

        self.assertIn("DATE(disease.collection_time) AS 日期", sql)
        self.assertIn("GROUP BY DATE(disease.collection_time)", sql)

    def test_disease_one_kilometer_bucket_compiles(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="top_groups",
                metrics=("count",),
                dimensions=("pile_segment_1000m",),
                filters=(),
                output_projection=(),
                order_by=({"field": "count", "direction": "desc"},),
                limit=5,
                required_assertions=(),
                question="每1公里一段统计病害高发区间",
            )
        )

        self.assertIn("FLOOR(disease.pile_distance / 1000) * 1000", sql)
        self.assertIn("AS 桩号分段", sql)
        self.assertIn("GROUP BY CONCAT(", sql)

    def test_pile_number_between_compiles_as_distance_range(self):
        """“K120 到 K125”是两个边界都要保留的范围过滤，不是单点桩号。"""

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(
                binding("pile_number", "between", ("K120+000", "K125+000")),
            ),
            output_projection=("problem_type_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="K120到K125之间的病害类型和数量",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("disease.pile_distance BETWEEN 120000 AND 125000", sql)

    def test_pile_number_in_list_compiles_each_point(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                binding("pile_number", "in", ["K16+500", "K33+500"]),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="K16+500 和 K33+500 两处的病害数量",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("disease.pile_distance IN (16500, 33500)", sql)

    def test_pile_number_between_requires_two_edges(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(binding("pile_number", "between", ("K120+000",)),),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="只给一个桩号边界",
        )
        with self.assertRaisesRegex(
            UnsupportedQuerySpec, "桩号区间必须提供两个边界值"
        ):
            compile_spec_mdl(spec)

    def test_pile_number_numeric_binding_stays_meters(self):
        """值解析器归一后的数值单位是米，编译器不能再乘 1000。"""

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(binding("pile_number", "between", (120000, 125000)),),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="K120到K125之间的病害数量",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("disease.pile_distance BETWEEN 120000 AND 125000", sql)

    def test_pile_number_zero_start_is_accepted(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(binding("pile_number", "between", (0, 50000)),),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="K0到K50之间的病害数量",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("disease.pile_distance BETWEEN 0 AND 50000", sql)

    def test_raw_pile_distance_grouping_points_to_registered_segments(self):
        """按原始桩号数值分组（如"每300米一段"）要给出可执行的替代维度。"""

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("pile_distance",),
            filters=(),
            output_projection=("pile_distance", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=3,
            required_assertions=(),
            question="每300米一段统计病害数量",
        )
        with self.assertRaisesRegex(
            UnsupportedQuerySpec, "pile_segment_500m"
        ):
            compile_spec_mdl(spec)

    def test_luge_name_grouping_points_to_registered_segments(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("luge_name",),
            filters=(),
            output_projection=("luge_name", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=3,
            required_assertions=(),
            question="哪些公里段的病害最集中",
        )
        with self.assertRaisesRegex(
            UnsupportedQuerySpec, "里程区段编号"
        ):
            compile_spec_mdl(spec)

    def test_history_damage_interval_is_rejected_when_all_rows_are_empty(self):
        with self.assertRaisesRegex(UnsupportedQuerySpec, "全部为空"):
            compile_spec_mdl(
                QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="disease",
                    view="distribution",
                    metrics=("count",),
                    dimensions=("history_damage_interval",),
                    filters=(),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=(),
                    question="按损坏程度统计病害数量",
                )
            )

    def test_year_and_quarter_time_dimensions_compile(self):
        year_sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("collection_time_year",),
                filters=(),
                output_projection=(),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按年份统计病害数量",
            )
        )
        quarter_sql = compile_spec_mdl(
            QuerySpec(
                id="q2",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("collection_time_quarter",),
                filters=(),
                output_projection=(),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按季度统计病害数量",
            )
        )

        self.assertIn("DATE_FORMAT(disease.collection_time, '%Y') AS 年份", year_sql)
        self.assertIn("QUARTER(disease.collection_time)", quarter_sql)
        self.assertIn("AS 季度", quarter_sql)

    def test_week_time_dimension_compiles(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="trend",
                metrics=("count",),
                dimensions=("week",),
                filters=(),
                output_projection=(),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按周统计病害数量",
            )
        )

        self.assertIn("DATE_FORMAT(disease.collection_time, '%x')", sql)
        self.assertIn("DATE_FORMAT(disease.collection_time, '%v')", sql)
        self.assertIn("AS 周", sql)

    def test_month_dimension_aliases_compile_consistently(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("collection_month",),
                filters=(),
                output_projection=("collection_month", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按月份统计病害数量",
            )
        )

        self.assertIn("DATE_FORMAT(disease.collection_time, '%Y-%m') AS 月份", sql)

    def test_disease_identifier_filter_joins_owning_task(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("problem_type_name",),
                filters=(binding("task_number", "=", "XJJL202611040003"),),
                output_projection=("problem_type_name", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="工单任务检出的病害分布",
            )
        )

        self.assertIn("JOIN patrol ON patrol.id = disease.patrol_id", sql)
        self.assertIn("patrol.task_number = 'XJJL202611040003'", sql)
        self.assertIn("GROUP BY COALESCE(disease.problem_type_name", sql)

    def test_time_between_uses_half_open_interval(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="count",
                metrics=("count",),
                dimensions=(),
                filters=(
                    binding(
                        "collection_time",
                        "between",
                        ("2026-11-03 00:00:00", "2026-11-04 00:00:00"),
                    ),
                ),
                output_projection=(),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="11月3日病害数量",
            )
        )

        self.assertIn(
            "disease.collection_time >= '2026-11-03 00:00:00'",
            sql,
        )
        self.assertIn(
            "disease.collection_time < '2026-11-04 00:00:00'",
            sql,
        )
        self.assertNotIn("BETWEEN", sql)

    def test_route_level_uses_route_ledger_and_display_labels(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("road_level",),
                filters=(),
                output_projection=("road_level", "count"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按道路等级统计病害数量",
            )
        )

        self.assertIn("route.level", sql)
        self.assertIn("国道", sql)
        self.assertIn("道路等级", sql)
        self.assertIn("JOIN route ON disease.routes_id = route.id", sql)
        self.assertIn("GROUP BY CASE route.level", sql)

    def test_disease_distribution_uses_calculated_disease_fields(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(
                binding("route_name", "=", "route-1"),
                binding("pile_number", ">=", "K10+000"),
                binding("pile_number", "<=", "K20+000"),
            ),
            output_projection=("problem_type_name", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("disease.problem_type_name", sql)
        self.assertNotIn("damage_dict.problem_type_name", sql)
        self.assertIn("route.id = 'route-1'", sql)
        self.assertIn("disease.pile_distance >= 10000", sql)
        self.assertIn("disease.pile_distance <= 20000", sql)

    def test_road_surface_distribution_labels_empty_value(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("road_surface_type",),
            filters=(),
            output_projection=("road_surface_type", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("NULLIF(disease.road_surface_type, '')", sql)
        self.assertIn("未填写", sql)

    def test_upstream_route_names_filter_by_display_column(self):
        spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "sum_area"),
            dimensions=("route_name",),
            filters=(binding("route_name", "in", ["路线A", "路线B"]),),
            output_projection=("route_name", "count", "sum_area"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn(
            "disease.route_name IN ('路线A', '路线B')",
            sql,
        )
        self.assertNotIn("route.id IN", sql)

    def test_dictionary_expansion_list_compiles_as_in_condition(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("up_or_down",),
            filters=(
                binding(
                    "problem_type_name",
                    "=",
                    ["横向裂缝", "网状裂缝", "纵向裂缝"],
                ),
            ),
            output_projection=("up_or_down", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn(
            "disease.problem_type_name IN ('横向裂缝', '网状裂缝', '纵向裂缝')",
            sql,
        )
        self.assertNotIn("LIKE", sql)

    def test_patrol_groups_by_equipment_and_route(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("count",),
            dimensions=("equipment_id", "route_name"),
            filters=(
                binding("create_time", ">=", "2026-11-01 00:00:00"),
                binding("create_time", "<", "2026-12-01 00:00:00"),
            ),
            output_projection=("equipment_id", "route_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("patrol.equipment_id", sql)
        self.assertIn("JOIN patrol_route", sql)
        self.assertIn("COUNT(*) AS 数量", sql)
        self.assertIn("GROUP BY patrol.equipment_id, route.short_name", sql)

    def test_patrol_count_task_alias_compiles_to_count(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("count_task",),
                dimensions=("equipment_id",),
                filters=(),
                output_projection=("equipment_id", "count_task"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="每台设备完成任务次数",
            )
        )

        self.assertIn("COUNT(*) AS 数量", sql)

    def test_dimension_list_without_metric_returns_distinct_dimensions(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("route_name",),
            filters=(),
            output_projection=("route_name",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn("SELECT route.short_name AS 路线名称", sql)
        self.assertNotIn("COUNT(*)", sql)
        self.assertIn("GROUP BY route.short_name", sql)

    def test_patrol_top_preserves_all_order_keys(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="top",
            metrics=("patrol_mileage",),
            dimensions=(
                "patrol_name",
                "patrol_number",
                "patrol_mileage",
                "create_time",
            ),
            filters=(),
            output_projection=(
                "patrol_name",
                "patrol_number",
                "patrol_mileage",
                "create_time",
            ),
            order_by=(
                {"field": "create_time", "direction": "desc"},
                {"field": "patrol_mileage", "direction": "desc"},
            ),
            limit=1,
            required_assertions=(),
            question="结构化子问题",
        )
        sql = compile_spec_mdl(spec)
        self.assertIn(
            "ORDER BY patrol.create_time DESC, SUM(patrol.patrol_mileage) DESC",
            sql,
        )
        self.assertIn("LIMIT 1", sql)
        self.assertNotIn("GROUP BY patrol.patrol_mileage", sql)

    def test_business_unavailable_field_is_rejected_before_sql(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(binding("lane_index", "=", "2"),),
            output_projection=("problem_type_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )
        with self.assertRaisesRegex(UnsupportedQuerySpec, "车道"):
            compile_spec_mdl(spec)
