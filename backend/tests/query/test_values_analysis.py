import unittest
from datetime import datetime
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.contracts import ValueBinding
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.values import resolve_value


class ValueAnalysisTests(unittest.TestCase):
    def test_next_month_exclusive_boundary_uses_range_end(self):
        value = resolve_value(
            "create_time",
            "<",
            "下月第一天",
            "华容－常宁高速公路在本月的巡检任务完成情况如何",
            "mysql",
        )

        now = datetime.now()
        self.assertEqual(
            value.storage_value,
            (
                f"{now.year + (1 if now.month == 12 else 0):04d}-"
                f"{1 if now.month == 12 else now.month + 1:02d}-01 00:00:00"
            ),
        )
        self.assertEqual(value.op, "<")

    def test_patrol_status_uses_patrol_dictionary(self):
        value = resolve_value(
            "patrol_status",
            "=",
            "已完成",
            "只看已完成的巡检任务",
            "mysql",
            subject="patrol",
        )

        self.assertEqual(value.storage_value, "15")
        self.assertEqual(value.display_value, "已完成")
        self.assertEqual(
            value.resolver,
            "enum:ai_maintain_patrol.patrol_status",
        )

    def test_exact_date_filter_uses_its_own_raw_value(self):
        question = "长沙－芷江高速公路11月2日与3日的病害对比情况"

        third = resolve_value(
            "collection_time",
            ">=",
            "2026-11-03",
            question,
            "mysql",
        )
        end = resolve_value(
            "collection_time",
            "<=",
            "2026-11-03",
            question,
            "mysql",
        )
        exclusive = resolve_value(
            "collection_time",
            "<",
            "2026-11-03",
            question,
            "mysql",
        )

        self.assertEqual(third.storage_value, "2026-11-03 00:00:00")
        self.assertEqual(end.storage_value, "2026-11-04 00:00:00")
        self.assertEqual(end.op, "<")
        self.assertEqual(exclusive.storage_value, "2026-11-03 00:00:00")
        self.assertEqual(exclusive.op, "<")

    @patch("app.query.values.route_resolver._resolve_route")
    def test_route_name_resolves_to_storage_id(self, resolve_route):
        resolve_route.return_value = {
            "id": "route-1",
            "short_name": "长沙－芷江高速公路",
        }
        value = resolve_value(
            "route_name",
            "=",
            "长沙－芷江高速公路",
            "长沙－芷江高速公路病害数量趋势",
            "mysql",
        )
        self.assertEqual(value.storage_value, "route-1")
        self.assertEqual(value.display_value, "长沙－芷江高速公路")
        self.assertEqual(value.resolver, "route_dictionary")

    @patch("app.query.values.route_resolver._resolve_route")
    def test_multiple_routes_resolve_each_value(self, resolve_route):
        resolve_route.side_effect = lambda text: {
            "id": f"id-{text}",
            "short_name": text,
        }

        value = resolve_value(
            "route_name",
            "in",
            ["S20外环高速", "G60沪昆高速"],
            "展示两条高速的月度病害趋势",
            "mysql",
        )

        self.assertEqual(
            value.storage_value,
            ["S20外环高速", "G60沪昆高速"],
        )
        self.assertEqual(value.display_value, "S20外环高速、G60沪昆高速")
        self.assertEqual(value.op, "in")
        self.assertEqual(resolve_route.call_count, 2)

    def test_damage_type_resolution_preserves_display_and_source(self):
        value = resolve_value(
            "problem_type_name",
            "=",
            "泛油",
            "泛油有多少处？",
            "mysql",
        )
        self.assertEqual(value.storage_value, "泛油")
        self.assertEqual(value.display_value, "泛油")
        self.assertEqual(value.source, "user_explicit")
        self.assertEqual(value.resolver, "damage_type_dictionary")

    def test_disease_category_and_severity_use_database_dictionary_codes(self):
        category = resolve_value(
            "category_name",
            "=",
            "交安事件",
            "调出来一下交安事件病害的记录",
            "mysql",
            subject="disease",
        )
        severity = resolve_value(
            "damage_type",
            "=",
            "重度",
            "重度病害数量有多少条",
            "mysql",
            subject="disease",
        )

        self.assertEqual(category.storage_value, "15")
        self.assertEqual(
            category.resolver,
            "enum:ai_problem.categories_type",
        )
        self.assertEqual(severity.storage_value, "10")
        self.assertEqual(
            severity.resolver,
            "enum:ai_problem.damage_type",
        )

    def test_umbrella_damage_type_expands_to_dictionary_values(self):
        value = resolve_value(
            "problem_type_name",
            "=",
            "裂缝",
            "裂缝有多少处？",
            "mysql",
        )
        self.assertEqual(value.display_value, "裂缝")
        self.assertIn("横向裂缝", value.storage_value)
        self.assertIn("纵向裂缝", value.storage_value)
        self.assertIn("网状裂缝", value.storage_value)
        self.assertEqual(value.resolver, "entity_family:crack")

    def test_damage_type_list_resolves_each_entity(self):
        value = resolve_value(
            "problem_type_name",
            "=",
            ["裂缝", "坑槽"],
            "裂缝和坑槽各自在不同道路上的病害密度",
            "mysql",
        )

        self.assertIn("横向裂缝", value.storage_value)
        self.assertIn("纵向裂缝", value.storage_value)
        self.assertIn("坑槽", value.storage_value)
        self.assertTrue(value.resolver.startswith("entity_family:"))

    def test_damage_type_list_keeps_explicit_member_provenance(self):
        value = resolve_value(
            "problem_type_name",
            "in",
            ["坑槽", "纵向裂缝"],
            "坑槽和纵向裂缝分别在上行、下行各有多少处？",
            "mysql",
        )

        self.assertEqual(value.source, "user_explicit")

    def test_month_boundaries_keep_explicit_time_provenance(self):
        start = resolve_value(
            "collection_time",
            ">=",
            "2026-10-01",
            "2026年10月，长沙－芷江高速的坑槽有多少处？",
            "mysql",
        )
        end = resolve_value(
            "collection_time",
            "<",
            "2026-11-01",
            "2026年10月，长沙－芷江高速的坑槽有多少处？",
            "mysql",
        )

        self.assertEqual(start.source, "user_explicit")
        self.assertEqual(end.source, "user_explicit")

    def test_quarter_value_is_normalized_to_exact_time_bounds(self):
        value = resolve_value(
            "create_time",
            "between",
            "Q3",
            "统计第三季度不同病害在不同道路上的新增情况",
            "mysql",
        )
        self.assertEqual(value.resolver, "time_range")
        self.assertEqual(
            value.storage_value,
            ("2026-07-01 00:00:00", "2026-10-01 00:00:00"),
        )

    def test_unspecified_year_day_filter_uses_current_business_year(self):
        start = resolve_value(
            "create_time",
            ">=",
            "2023-11-02 00:00:00",
            "11月2日巡检了哪些线路？",
            "mysql",
        )
        end = resolve_value(
            "create_time",
            "<",
            "2023-11-03 00:00:00",
            "11月2日巡检了哪些线路？",
            "mysql",
        )
        self.assertEqual(start.storage_value, "2026-11-02 00:00:00")
        self.assertEqual(end.storage_value, "2026-11-03 00:00:00")

    def test_relative_three_month_value_becomes_exact_bounds(self):
        value = resolve_value(
            "create_time",
            ">=",
            "3 months ago",
            "近三月有多少巡检记录？",
            "mysql",
        )
        self.assertEqual(value.op, "between")
        self.assertEqual(len(value.storage_value), 2)
        self.assertEqual(value.resolver, "time_range")

    def test_relative_two_month_value_becomes_exact_bounds(self):
        value = resolve_value(
            "collection_time",
            ">=",
            "2个月前",
            "计算最近两个月裂缝和坑槽的病害密度",
            "mysql",
        )
        self.assertEqual(value.op, "between")
        self.assertEqual(len(value.storage_value), 2)
        self.assertEqual(value.resolver, "time_range")

    def test_exclusive_next_year_boundary_is_not_rebased(self):
        value = resolve_value(
            "create_time",
            "<",
            "2027-01-01",
            "包头-茂名在2026年各月的巡检次数分别是多少？",
            "mysql",
        )

        self.assertEqual(value.storage_value, "2027-01-01 00:00:00")
        self.assertEqual(value.op, "<")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_patrol_device_dimension_uses_populated_equipment_id(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "巡检设备次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["device_code"],
                    "filters": [],
                    "output_projection": ["device_code", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }
        plan = analyze_single_turn(
            "巡检设备分别完成了多少次巡检？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(plan.nodes[0].spec.dimensions, ("equipment_id",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_device_question_accepts_structured_distribution(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "巡检设备次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["equipment_id"],
                    "filters": [],
                    "output_projection": ["equipment_id", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }
        plan = analyze_single_turn(
            "2026 年 11 月，巡检设备分别完成了多少次巡检？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        spec = plan.nodes[0].spec
        self.assertEqual(spec.view, "distribution")
        self.assertEqual(spec.dimensions, ("equipment_id",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_detail_limit_defaults_when_model_omits_limit(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "列出病害明细",
            "intent_family": "detail",
            "views": [
                {
                    "id": "q1",
                    "subject": "disease",
                    "view": "list",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": ["id"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }
        plan = analyze_single_turn(
            "把具体的数据调出来",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(plan.nodes[0].spec.limit, 500)

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_distribution_limit_without_value_stays_unbounded(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "路线指标",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }
        plan = analyze_single_turn(
            "按路线分组统计每条路线的病害总数",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertIsNone(plan.nodes[0].spec.limit)

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_ai_reliability_contract_uses_threshold_total_and_post_calc(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "评估AI识别可靠性",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1_low_confidence",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [
                        {
                            "field": "confidence",
                            "op": "<",
                            "raw_value": 0.7,
                        }
                    ],
                    "output_projection": ["count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q2_total_count",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": ["count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q3",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1_low_confidence", "q2_total_count"],
                },
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="confidence",
            op="<",
            storage_value="0.7",
            display_value="0.7",
            source="business_rule",
            resolver="numeric:confidence",
            raw_text="0.7",
        )
        plan = analyze_single_turn(
            "AI识别的病害类型，靠谱吗？有多少是认不出来的？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(len(plan.nodes), 3)
        low, total, calculation = plan.nodes
        self.assertEqual(low.spec.filters[0].field, "confidence")
        self.assertEqual(low.spec.filters[0].storage_value, "0.7")
        self.assertEqual(low.spec.filters[0].op, "<")
        self.assertEqual(total.spec.view, "count")
        self.assertEqual(calculation.node_type, "post_calc")
        self.assertEqual(calculation.depends_on, (low.id, total.id))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_density_contract_adds_route_length_metric(
        self,
        chat_json,
        resolve,
    ):
        chat_json.return_value = {
            "final_goal": "裂缝月份路线密度",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q3",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": [
                        "count",
                        "sum_disease_length",
                        "route_length_km",
                    ],
                    "dimensions": ["route_name"],
                    "filters": [{"field": "problem_type_name", "op": "=", "raw_value": "裂缝"}],
                    "output_projection": [
                        "route_name",
                        "count",
                        "route_length_km",
                    ],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "question": "这些路的病害密度怎么样？",
                },
                {
                    "id": "q4",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q3"],
                    "question": "计算病害密度",
                },
            ],
        }
        resolve.return_value = ValueBinding(
            field="problem_type_name",
            op="=",
            storage_value=["横向裂缝", "纵向裂缝"],
            display_value="裂缝",
            source="user_explicit",
            resolver="damage_type_dictionary_expand",
            raw_text="裂缝",
        )
        plan = analyze_single_turn(
            "裂缝集中在哪几个月？哪些路？这些路的病害密度怎么样？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertIn("route_length_km", plan.nodes[0].spec.metrics)
        calculation = next(node for node in plan.nodes if node.node_type == "post_calc")
        self.assertEqual(calculation.depends_on, (plan.nodes[0].id,))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_density_plan_uses_each_structured_disease_dataset(
        self,
        chat_json,
        resolve,
    ):
        chat_json.return_value = {
            "final_goal": "裂缝和坑槽分路线密度",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "crack",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [{"field": "problem_type_name", "op": "=", "raw_value": "裂缝"}],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                },
                {
                    "id": "pothole",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [{"field": "problem_type_name", "op": "=", "raw_value": "坑槽"}],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                },
                {
                    "id": "q3",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["crack", "pothole"],
                },
            ],
        }
        resolve.side_effect = [
            ValueBinding(
                field="problem_type_name",
                op="=",
                storage_value=["横向裂缝", "网状裂缝", "纵向裂缝"],
                display_value="裂缝",
                source="user_explicit",
                resolver="damage_type_dictionary_expand",
                raw_text="裂缝",
            ),
            ValueBinding(
                field="problem_type_name",
                op="=",
                storage_value="坑槽",
                display_value="坑槽",
                source="user_explicit",
                resolver="damage_type_dictionary",
                raw_text="坑槽",
            ),
        ]

        plan = analyze_single_turn(
            "计算最近两个月，裂缝和坑槽各自在不同道路上的病害密度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        calculation = next(node for node in plan.nodes if node.node_type == "post_calc")
        self.assertEqual(calculation.depends_on, ("crack", "pothole"))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_route_metrics_come_from_structured_model_output(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "路线多指标",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": [
                        "count",
                        "total_area",
                        "average_area",
                        "route_length_km",
                    ],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": [
                        "route_name",
                        "count",
                        "total_area",
                        "average_area",
                        "route_length_km",
                    ],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                },
            ],
        }
        plan = analyze_single_turn(
            "按路线分组，统计每条路线的病害总数、病害总面积、平均单处病害面积，并关联获取该路线的长度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(len(plan.nodes), 1)
        spec = plan.nodes[0].spec
        self.assertEqual(spec.dimensions, ("route_name",))
        self.assertEqual(
            spec.metrics,
            ("count", "total_area", "average_area", "route_length_km"),
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_analysis_returns_structured_query_plan(
        self,
        chat_json,
        resolve,
    ):
        chat_json.return_value = {
            "final_goal": "统计泛油数量",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "泛油",
                        }
                    ],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }
        resolve.return_value = ValueBinding(
            field="problem_type_name",
            op="=",
            storage_value="泛油",
            display_value="泛油",
            source="user_explicit",
            resolver="damage_type_dictionary",
            raw_text="泛油",
        )
        plan = analyze_single_turn(
            "泛油有多少处？",
            "mysql",
            PhysicalSchemaView(columns=frozenset({("disease", "problem_base_id")})),
            FieldReachSet(hits=()),
        )
        spec = plan.nodes[0].spec
        self.assertEqual(spec.view, "count")
        self.assertEqual(spec.filters[0].display_value, "泛油")
        self.assertTrue(spec.filters[0].resolver)
