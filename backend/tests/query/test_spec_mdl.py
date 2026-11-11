import unittest

import sqlglot
from sqlglot import exp

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


def join_equality_pairs(sql, joined_model):
    parsed = sqlglot.parse_one(sql, read="mysql")
    for join in parsed.args.get("joins", ()):
        if str(join.this.name or "") != joined_model:
            continue
        on = join.args.get("on")
        if on is None:
            return ()
        pairs = []
        for equality in on.find_all(exp.EQ):
            if not (
                isinstance(equality.this, exp.Column)
                and isinstance(equality.expression, exp.Column)
            ):
                continue
            pairs.append(
                frozenset(
                    {
                        (
                            str(equality.this.table or ""),
                            str(equality.this.name or ""),
                        ),
                        (
                            str(equality.expression.table or ""),
                            str(equality.expression.name or ""),
                        ),
                    }
                )
            )
        return tuple(pairs)
    raise AssertionError(f"missing JOIN for model {joined_model}")


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

    def test_disease_length_output_uses_the_same_effective_length_as_filter(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(binding("disease_length", ">", 4),),
                output_projection=(
                    "problem_number",
                    "disease_length",
                    "collection_time",
                ),
                order_by=({"field": "collection_time", "direction": "desc"},),
                limit=500,
                required_assertions=(),
                question="列出长度大于4米的病害明细",
            )
        )

        self.assertIn(
            "disease.effective_disease_length > 4",
            sql,
        )
        self.assertIn(
            "disease.effective_disease_length AS 实际病害长度",
            sql,
        )
        self.assertNotIn(
            "disease.disease_length AS 实际病害长度",
            sql,
        )

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

    def test_patrol_list_can_output_task_number(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=("task_number",),
                order_by=({"field": "collection_time", "direction": "desc"},),
                limit=5,
                required_assertions=(),
                question="列出最近5次巡检任务的任务编号",
            )
        )

        self.assertIn("patrol.task_number AS 任务编号", sql)

    def test_disease_list_projects_and_orders_confidence(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=(
                    "problem_number",
                    "problem_type_name",
                    "route_name",
                    "pile_number",
                    "collection_time",
                    "confidence",
                ),
                order_by=({"field": "confidence", "direction": "asc"},),
                limit=500,
                required_assertions=("preserve_user_scope",),
                question="输出病害明细和 AI 识别置信度，按置信度升序",
            )
        )

        self.assertIn("disease.confidence AS AI置信度", sql)
        self.assertIn("ORDER BY disease.confidence ASC", sql)

    def test_disease_detail_displays_enum_label_instead_of_storage_code(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=(
                    "problem_number",
                    "up_or_down",
                ),
                order_by=(),
                limit=10,
                required_assertions=("preserve_user_scope",),
                question="列出病害编号和上下行方向",
            )
        )

        self.assertIn("CASE disease.up_or_down", sql)
        self.assertIn("THEN '上行'", sql)
        self.assertIn("THEN '下行'", sql)
        self.assertIn("AS 上下行", sql)
        self.assertNotIn("disease.up_or_down AS 上下行", sql)

    def test_disease_category_name_filters_on_storage_code(
        self,
    ):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(binding("category_name", "=", "15"),),
                output_projection=(
                    "problem_number",
                    "problem_type_name",
                    "category_name",
                ),
                order_by=(),
                limit=500,
                required_assertions=("preserve_user_scope",),
                question="调出来一下交安事件病害的记录",
            )
        )

        self.assertIn("disease.categories_type = '15'", sql)
        self.assertIn("CASE disease.categories_type", sql)
        self.assertIn("AS 病害大类", sql)

    def test_disease_distribution_can_group_by_category_name(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("category_name",),
                filters=(),
                output_projection=("category_name", "count"),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
                question="按病害大类统计数量",
            )
        )

        self.assertIn("CASE disease.categories_type", sql)
        self.assertIn("AS 病害大类", sql)
        self.assertIn("GROUP BY CASE disease.categories_type", sql)

    def test_disease_damage_severity_uses_authoritative_dictionary(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("damage_type",),
                filters=(binding("damage_type", "=", "10"),),
                output_projection=("damage_type", "count"),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
                question="重度病害有多少条",
            )
        )

        self.assertIn("disease.damage_type = '10'", sql)
        self.assertIn("AS 损坏程度", sql)
        self.assertIn("GROUP BY", sql)

    def test_patrol_list_projects_registered_fields_beyond_hand_written_map(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=(
                    "patrol_number",
                    "patrol_type",
                    "route_name",
                    "up_or_down",
                ),
                order_by=({"field": "create_time", "direction": "desc"},),
                limit=1,
                required_assertions=("recent_window", "preserve_user_scope"),
                question="把最近一次巡查记录调出来",
            )
        )

        self.assertIn("CASE patrol.patrol_type", sql)
        self.assertIn("THEN '定期巡查'", sql)
        self.assertIn("AS 巡查类型", sql)
        self.assertIn("route.short_name AS 路线名称", sql)
        self.assertIn("CASE patrol.up_or_down", sql)
        self.assertIn("THEN '上行'", sql)
        self.assertIn("AS 上下行", sql)
        self.assertIn("LEFT JOIN patrol_route", sql)
        self.assertIn("LEFT JOIN route", sql)

    def test_patrol_route_filter_uses_dictionary_id_against_route_primary_key(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("count",),
                dimensions=("equipment_id",),
                filters=(
                    ValueBinding(
                        field="route_name",
                        op="=",
                        storage_value="51c9c71ea0f811f1a6310242ac120002",
                        display_value="上海-昆明",
                        source="user_explicit",
                        resolver="route_dictionary",
                        raw_text="上海-昆明高速",
                    ),
                ),
                output_projection=("equipment_id", "count"),
                order_by=({"field": "count", "direction": "desc"},),
                limit=None,
                required_assertions=("preserve_user_scope",),
                question="上海-昆明高速上各设备分别完成了多少次任务",
            )
        )

        self.assertIn(
            "route.id = '51c9c71ea0f811f1a6310242ac120002'",
            sql,
        )
        self.assertIn("EXISTS", sql)
        self.assertIn("INNER JOIN route", sql)
        self.assertIn("patrol_route.patrol_id = patrol.id", sql)
        self.assertNotIn("patrol.route_name", sql)

    def test_patrol_distribution_uses_linked_disease_count(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("linked_disease_count",),
                dimensions=("task_number",),
                filters=(binding("collection_time", ">=", "2026-11-05"),),
                output_projection=("task_number", "linked_disease_count"),
                order_by=({"field": "task_number", "direction": "asc"},),
                limit=None,
                required_assertions=(),
                question="列出巡检任务编号及该任务检出的病害总数",
            )
        )

        self.assertIn("patrol.task_number AS 任务编号", sql)
        self.assertIn("COUNT(DISTINCT disease.id) AS 任务关联病害数", sql)
        self.assertIn(
            "LEFT JOIN disease ON disease.patrol_id = patrol.id "
            "AND disease.del_flag = 0",
            sql,
        )
        self.assertNotIn("COUNT(*) AS 数量", sql)

    def test_patrol_count_stays_distinct_when_grouped_by_route(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("count",),
                dimensions=("equipment_id", "route_name"),
                filters=(),
                output_projection=(
                    "equipment_id",
                    "route_name",
                    "count",
                ),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
                question="每台巡检设备在各条路线上分别完成多少次任务",
            )
        )

        self.assertIn("COUNT(DISTINCT patrol.id) AS 数量", sql)

    def test_disease_task_evidence_uses_real_task_number(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("task_number",),
            filters=(
                binding("problem_type_name", "=", "坑槽"),
                binding("area", ">", 4),
            ),
            output_projection=("task_number", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="按巡检任务统计面积大于4平方米的坑槽",
        )

        sql = compile_spec_mdl(spec)

        self.assertIn(
            frozenset(
                {
                    ("patrol", "id"),
                    ("disease", "patrol_id"),
                }
            ),
            join_equality_pairs(sql, "patrol"),
        )
        self.assertIn("patrol.task_number AS 任务编号", sql)
        self.assertIn("GROUP BY patrol.task_number", sql)
        self.assertNotIn("disease.patrol_number AS 任务编号", sql)

    def test_patrol_mileage_detail_uses_registered_km_unit(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=(
                    "patrol_number",
                    "patrol_status",
                    "patrol_mileage",
                ),
                order_by=(),
                limit=500,
                required_assertions=(),
                question="列出巡检任务编号、状态和实际完成里程",
            )
        )

        self.assertIn(
            "patrol.patrol_mileage / 1000.0 AS 巡检里程",
            sql,
        )

    def test_task_route_dimension_is_aggregated_without_duplicating_task(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("linked_disease_count",),
                dimensions=("task_number", "route_name"),
                filters=(),
                output_projection=(
                    "task_number",
                    "route_name",
                    "linked_disease_count",
                ),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="每个巡检任务一行，并汇总其覆盖路线和关联病害数",
            )
        )

        self.assertIn(
            "GROUP_CONCAT(DISTINCT route.short_name "
            "ORDER BY route.short_name SEPARATOR '、') AS 路线名称",
            sql,
        )
        self.assertIn("GROUP BY patrol.task_number", sql)
        self.assertNotIn(
            "GROUP BY patrol.task_number, route.short_name",
            sql,
        )

    def test_patrol_status_dimension_uses_display_labels(self):
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

    def test_grouped_metrics_are_not_dropped_by_output_projection(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("problem_type_name",),
                filters=(),
                output_projection=("problem_type_name",),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="按病害类型统计数量",
            )
        )

        self.assertIn("COUNT(*) AS 数量", sql)
        self.assertIn("GROUP BY", sql)

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

    def test_disease_two_kilometer_bucket_compiles(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="top_groups",
                metrics=("count",),
                dimensions=("pile_segment_2000m",),
                filters=(),
                output_projection=(),
                order_by=({"field": "count", "direction": "desc"},),
                limit=3,
                required_assertions=(),
                question="按每2公里分段统计病害高发区间",
            )
        )

        self.assertIn("FLOOR(disease.pile_distance / 2000) * 2000", sql)
        self.assertIn("+ 2000", sql)
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
            UnsupportedQuerySpec,
            r"pile_segment_<N>m.*pile_segment_2000m",
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

        self.assertIn("EXISTS", sql)
        self.assertIn("disease.patrol_id", sql)
        self.assertIn("patrol.id", sql)
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
        self.assertIn("行政等级", sql)
        self.assertIn("JOIN route ON disease.routes_id = route.id", sql)
        self.assertIn("GROUP BY CASE route.level", sql)

    def test_joined_model_filter_is_in_where_not_join_condition(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("route_length_km",),
                dimensions=("route_name",),
                filters=(
                    ValueBinding(
                        field="route_name",
                        op="=",
                        storage_value="route-1",
                        display_value="路线一",
                        source="user_explicit",
                        resolver="route_dictionary",
                        raw_text="路线一",
                    ),
                ),
                output_projection=("route_name", "route_length_km"),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
                question="统计指定路线的病害数量",
            )
        )

        self.assertIn(
            "LEFT JOIN route ON disease.routes_id = route.id",
            sql,
        )
        self.assertIn("route.id = 'route-1'", sql)
        self.assertNotIn(
            "ON disease.routes_id = route.id AND route.id",
            sql,
        )

    def test_disease_distribution_uses_calculated_disease_fields(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="route-1",
                    display_value="路线一",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="路线一",
                ),
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
        self.assertIn("LEFT JOIN patrol_route", sql)
        self.assertIn("COUNT(DISTINCT patrol.id) AS 数量", sql)
        self.assertIn("GROUP BY patrol.equipment_id, route.short_name", sql)

    def test_patrol_task_detail_keeps_tasks_without_route_relations(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("linked_disease_count",),
            dimensions=("task_number", "route_name"),
            filters=(
                binding("collection_time", ">=", "2026-11-05 00:00:00"),
                binding("collection_time", "<", "2026-11-06 00:00:00"),
            ),
            output_projection=(
                "task_number",
                "route_name",
                "linked_disease_count",
            ),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="列出当天巡检任务、覆盖路线和病害数",
        )

        sql = compile_spec_mdl(spec)

        self.assertIn("LEFT JOIN patrol_route", sql)
        self.assertIn("LEFT JOIN route", sql)

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

        self.assertIn("COUNT(DISTINCT patrol.id) AS 数量", sql)

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
        self.assertIn("ORDER BY patrol.create_time DESC", sql)
        self.assertIn("SUM(patrol.patrol_mileage) / 1000.0", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertNotIn("GROUP BY patrol.patrol_mileage", sql)

    def test_patrol_mileage_converts_stored_meters_to_km(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="patrol",
                view="distribution",
                metrics=("patrol_mileage",),
                dimensions=("equipment_id",),
                filters=(),
                output_projection=("equipment_id", "patrol_mileage"),
                order_by=({"field": "patrol_mileage", "direction": "desc"},),
                limit=None,
                required_assertions=(),
                question="每台设备累计巡检里程",
            )
        )

        self.assertIn("SUM(patrol.patrol_mileage) / 1000", sql)
        self.assertIn("AS 巡检里程", sql)

    def test_patrol_collection_time_uses_the_same_physical_time_in_all_roles(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=(),
            filters=(binding("collection_time", ">=", "2026-11-01 00:00:00"),),
            output_projection=("patrol_number", "collection_time"),
            order_by=({"field": "collection_time", "direction": "desc"},),
            limit=5,
            required_assertions=(),
            question="列出最近5次巡检，并显示巡检时间",
        )

        sql = compile_spec_mdl(spec)

        self.assertIn(
            "patrol.create_time >= '2026-11-01 00:00:00'",
            sql,
        )
        self.assertIn("patrol.create_time AS 巡检时间", sql)
        self.assertIn("ORDER BY patrol.create_time DESC", sql)

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
