import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.contracts import ValueBinding
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.services.plan_contract import build_step_contract


class QueryPlanningTests(unittest.TestCase):
    @patch("app.query.analysis.chat_json", side_effect=AssertionError("LLM should not be called"))
    def test_route_health_analysis_uses_deterministic_plan(self, _chat_json):
        plan = analyze_single_turn(
            "哪条路的健康状况最好？从哪几个维度判断？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.response_mode, "analysis")
        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertTrue(
            {
                "count",
                "total_area",
                "average_area",
                "route_length_km",
            }.issubset(set(plan.nodes[0].spec.metrics))
        )
        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    @patch("app.query.analysis.route_resolver._route_text_in_message", return_value="华容－常宁高速公路")
    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_completion_question_groups_status_without_inventing_filter(
        self,
        chat_json,
        resolve_value,
        _route_text,
    ):
        chat_json.return_value = {
            "final_goal": "华容-常宁本月巡检完成情况",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count", "sum_completed"],
                    "dimensions": ["patrol_status"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "华容－常宁高速公路",
                        },
                        {
                            "field": "patrol_status",
                            "op": "=",
                            "raw_value": "已完成",
                        },
                    ],
                    "output_projection": ["patrol_status", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        def resolve(field, op, raw_value, question, kind):
            if field == "route_name":
                return ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="route-id",
                    display_value="华容－常宁高速公路",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="华容－常宁高速公路",
                )
            return ValueBinding(
                field=field,
                op=op,
                storage_value=raw_value,
                display_value=str(raw_value),
                source="model_inference",
                resolver="time_range",
                raw_text=str(raw_value),
            )

        resolve_value.side_effect = resolve

        plan = analyze_single_turn(
            "华容－常宁高速公路在本月的巡检任务完成情况如何",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        spec = plan.nodes[0].spec
        self.assertEqual(spec.dimensions, ("patrol_status",))
        self.assertEqual(spec.metrics, ("count",))
        self.assertNotIn("patrol_status", {item.field for item in spec.filters})
        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    @patch("app.query.analysis.chat_json")
    def test_metric_objects_are_normalized_to_logical_metrics(self, chat_json):
        chat_json.return_value = {
            "final_goal": "任务次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": [{"field": "task_id", "agg": "count"}],
                    "dimensions": ["equipment_id"],
                    "filters": [],
                    "output_projection": ["equipment_id", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "每台设备分别完成多少次任务",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.metrics, ("count",))

    @patch("app.query.analysis.chat_json")
    def test_patrol_device_alias_uses_equipment_dimension(self, chat_json):
        chat_json.return_value = {
            "final_goal": "设备任务次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["device_id"],
                    "filters": [],
                    "output_projection": ["device_id", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "2026年11月每台巡检设备分别完成多少次巡检",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.dimensions, ("equipment_id",))
        self.assertEqual(plan.nodes[0].spec.output_projection, ("equipment_id", "count"))

    @patch("app.query.analysis.chat_json")
    def test_route_dependency_uses_display_name_contract(self, chat_json):
        chat_json.return_value = {
            "final_goal": "Top路线多指标",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "top",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["routes_id"],
                    "filters": [],
                    "output_projection": ["routes_id", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 5,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "stats",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "sum_area"],
                    "dimensions": ["routes_id"],
                    "filters": [],
                    "output_projection": ["routes_id", "count", "sum_area"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["top"],
                    "inputs": [
                        {
                            "from_node": "top",
                            "from_field": "routes_id",
                            "to_field": "routes_id",
                            "op": "in",
                        }
                    ],
                },
            ],
        }

        plan = analyze_single_turn(
            "病害数量最多的前5条路线分别有多少处、总面积多少平方米？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertEqual(plan.nodes[1].spec.dimensions, ("route_name",))
        binding = plan.nodes[1].spec.input_bindings[0]
        self.assertEqual(binding.from_field, "route_name")
        self.assertEqual(binding.to_field, "route_name")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_patrol_route_question_is_normalized_to_patrol_subject(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "11月2日巡检路线",
            "intent_family": "detail",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "route",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["route_name"],
                    "filters": [
                        {
                            "field": "patrol_date",
                            "op": ">=",
                            "raw_value": "2026-11-02",
                        }
                    ],
                    "output_projection": ["route_name"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="patrol_date",
            op=">=",
            storage_value="2026-11-02",
            display_value="2026-11-02",
            source="user_explicit",
            resolver="time_range",
            raw_text="2026-11-02",
        )

        plan = analyze_single_turn(
            "11月2日巡检了哪些线路？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.subject, "patrol")
        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertEqual(plan.nodes[0].spec.filters[0].field, "create_time")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_planner_order_alias_is_normalized(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "按数量降序",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["problem_type_name"],
                    "filters": [],
                    "output_projection": ["problem_type_name", "count"],
                    "order_by": [{"field": "count", "order": "desc"}],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "按病害类型统计数量并降序",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.order_by[0]["direction"], "desc")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_identifier_dictionary_normalizes_patrol_task_number(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "任务检出的病害分布",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计工单XJJL202611040003检出的病害分布",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["problem_type_name"],
                    "filters": [
                        {
                            "field": "work_order_id",
                            "op": "=",
                            "raw_value": "XJJL202611040003",
                        }
                    ],
                    "output_projection": ["problem_type_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="work_order_id",
            op="=",
            storage_value="XJJL202611040003",
            display_value="XJJL202611040003",
            source="user_explicit",
            resolver="identity:work_order_id",
            raw_text="XJJL202611040003",
        )

        plan = analyze_single_turn(
            "工单XJJL202611040003检出病害数量以及病害分布情况",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        filters = plan.nodes[0].spec.filters
        self.assertIn("task_number", {item.field for item in filters})
        self.assertNotIn("work_order_id", {item.field for item in filters})
        task = next(item for item in filters if item.field == "task_number")
        self.assertEqual(task.resolver, "identifier_dictionary:XJJL")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_analysis_action_is_normalized_to_post_calc(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "计算比较结果",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "analysis",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }
        plan = analyze_single_turn(
            "计算比较结果",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    def test_prior_result_top_question_must_be_post_calc(self):
        contract = build_step_contract(
            "统计各路线下病害类型数量，找出每条路线数量最多的类型",
            {
                "type": "data",
                "question": "基于q1的结果，找出每条路线数量最多的主要病害类型",
            },
            known_damage_types=["泛油", "纵向裂缝"],
        )
        self.assertEqual(contract["status"], "invalid")
        self.assertIn(
            "query_data_over_prior_result",
            {item["code"] for item in contract["violations"]},
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_analysis_preserves_explicit_post_calc_action(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "找出每条路线的主要病害",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计各路线下各病害类型数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "基于q1的结果，找出每条路线数量最多的主要病害类型",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": ["route_name", "problem_type_name", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 1,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                },
            ],
        }
        plan = analyze_single_turn(
            "找出每条路线数量最多的主要病害类型",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(plan.nodes[1].node_type, "post_calc")
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))
        self.assertEqual(plan.nodes[1].spec.view, "data")
        self.assertEqual(plan.nodes[1].spec.metrics, ())
        self.assertEqual(plan.nodes[1].spec.dimensions, ())

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_prior_group_result_top_is_normalized_to_post_calc(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "找出每条路线的主要病害",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计各路线下各病害类型数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q2",
                    "action": "query_data",
                    "question": "基于分组统计结果，找出每条路线下数量最多的病害类型",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": ["route_name", "problem_type_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                },
            ],
        }

        plan = analyze_single_turn(
            "按路线和病害类型分组，统计各路线下每种病害类型的数量，找出每条路线数量最多的主要病害类型",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_severity_question_requires_all_comparison_metrics(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "判断病害最严重的道路",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按道路统计病害数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "哪个道路的病害最严重？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertTrue(
            {"count", "total_area", "route_length_km"}.issubset(
                set(plan.nodes[0].spec.metrics)
            )
        )
        self.assertEqual(plan.nodes[1].node_type, "post_calc")
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_trend_question_adds_a_bounded_post_calculation(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "判断病害数量趋势",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按月统计该路线病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "长沙－芷江高速公路病害数量，最近是在增加还是在减少？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[1].node_type, "post_calc")
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))
