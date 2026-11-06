import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.contracts import QuerySpecError, ValueBinding
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.services.plan_contract import build_step_contract


class QueryPlanningTests(unittest.TestCase):
    @patch("app.query.analysis.chat_json")
    def test_auxiliary_subject_is_allowed_when_final_subject_matches(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "统计最近5次巡查中的坑槽数量",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "batches",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [
                        {"field": "create_time", "direction": "desc"}
                    ],
                    "limit": 5,
                    "required_assertions": [
                        "preserve_user_scope",
                        "recent_window",
                    ],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "counts",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["patrol_number"],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "坑槽",
                        }
                    ],
                    "output_projection": ["patrol_number", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["batches"],
                    "inputs": [
                        {
                            "from_node": "batches",
                            "from_field": "patrol_number",
                            "to_field": "patrol_number",
                            "op": "in",
                        }
                    ],
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.subject, "patrol")
        self.assertEqual(plan.nodes[-1].spec.subject, "disease")

    @patch("app.query.analysis.chat_json")
    def test_trend_intent_repairs_missing_terminal_analysis(self, chat_json):
        data_view = {
            "id": "q1",
            "action": "query_data",
            "subject": "disease",
            "view": "distribution",
            "metrics": ["count"],
            "dimensions": ["patrol_number"],
            "filters": [
                {
                    "field": "problem_type_name",
                    "op": "=",
                    "raw_value": "坑槽",
                }
            ],
            "output_projection": ["patrol_number", "count"],
            "order_by": [],
            "limit": None,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": [],
            "inputs": [],
        }
        first = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [data_view],
        }
        second = {
            **first,
            "views": [
                data_view,
                {
                    "id": "trend",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }
        chat_json.side_effect = [first, second]

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertEqual(plan.response_mode, "analysis")

    @patch("app.query.analysis.chat_json")
    def test_trend_analysis_consumes_all_data_nodes(self, chat_json):
        batch_view = {
            "id": "batches",
            "action": "query_data",
            "subject": "patrol",
            "view": "list",
            "metrics": [],
            "dimensions": ["patrol_number"],
            "filters": [],
            "output_projection": ["patrol_number"],
            "order_by": [{"field": "create_time", "direction": "desc"}],
            "limit": 5,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": [],
            "inputs": [],
        }
        count_view = {
            "id": "counts",
            "action": "query_data",
            "subject": "disease",
            "view": "distribution",
            "metrics": ["count"],
            "dimensions": ["patrol_number"],
            "filters": [
                {
                    "field": "problem_type_name",
                    "op": "=",
                    "raw_value": "坑槽",
                }
            ],
            "output_projection": ["patrol_number", "count"],
            "order_by": [],
            "limit": None,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": ["batches"],
            "inputs": [
                {
                    "from_node": "batches",
                    "from_field": "patrol_number",
                    "to_field": "patrol_number",
                    "op": "in",
                }
            ],
        }
        chat_json.return_value = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [
                batch_view,
                count_view,
                {
                    "id": "trend",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["counts"],
                    "inputs": [],
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[-1].depends_on, ("batches", "counts"))

    @patch("app.query.analysis.chat_json")
    def test_trend_series_uses_batch_axis_for_labels(self, chat_json):
        chat_json.return_value = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [
                {
                    "id": "batches",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [
                        {"field": "create_time", "direction": "desc"}
                    ],
                    "limit": 5,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "counts",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["patrol_number"],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "坑槽",
                        }
                    ],
                    "output_projection": ["patrol_number", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["batches"],
                    "inputs": [
                        {
                            "from_node": "batches",
                            "from_field": "patrol_number",
                            "to_field": "patrol_number",
                            "op": "in",
                        }
                    ],
                },
                {
                    "id": "trend",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["batches", "counts"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_series",
                        "operation": "subtract",
                        "operands": [
                            {
                                "name": "labels",
                                "source": {
                                    "node": "counts",
                                    "field": "patrol_number",
                                },
                            },
                            {
                                "name": "values",
                                "source": {
                                    "node": "counts",
                                    "field": "count",
                                },
                            },
                        ],
                        "precision": 6,
                    },
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        operands = {
            item.name: item
            for item in plan.nodes[-1].spec.calculation.operands
        }
        self.assertEqual(operands["labels"].source["node"], "batches")
        self.assertEqual(operands["values"].source["node"], "counts")

    @patch("app.query.analysis.chat_json")
    def test_limited_list_requires_order_before_execution(self, chat_json):
        chat_json.return_value = {
            "final_goal": "最近5次巡查",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "batches",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [],
                    "limit": 5,
                    "required_assertions": [
                        "preserve_user_scope",
                        "recent_window",
                    ],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        with self.assertRaisesRegex(QuerySpecError, "必须声明 order_by"):
            analyze_single_turn(
                "最近5次巡查",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                subject="patrol",
            )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_uncompilable_plan_is_fed_back_before_audit(
        self,
        chat_json,
        _audit,
    ):
        invalid = {
            "final_goal": "列出巡查设备",
            "intent_family": "detail",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["vehicle_id"],
                    "filters": [],
                    "output_projection": ["vehicle_id"],
                    "order_by": [],
                    "limit": 500,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                },
            ],
        }
        valid = {
            **invalid,
            "views": [
                {
                    **invalid["views"][0],
                    "dimensions": ["equipment_id"],
                    "output_projection": ["equipment_id"],
                }
            ],
        }
        chat_json.side_effect = [invalid, valid]

        plan = analyze_single_turn(
            "列出所有巡查设备",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(plan.nodes[0].spec.dimensions, ("equipment_id",))

    @patch("app.query.analysis.chat_json")
    def test_order_by_registered_metric_is_valid(self, chat_json):
        chat_json.return_value = {
            "final_goal": "按数量排序路线",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计数量并排序",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [
                        {"field": "count", "direction": "desc"}
                    ],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "按数量排序路线",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.order_by[0]["field"], "count")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_completion_question_accepts_model_status_distribution(
        self,
        chat_json,
        resolve_value,
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
                    "metrics": ["count"],
                    "dimensions": ["patrol_status"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "华容－常宁高速公路",
                        }
                    ],
                    "output_projection": ["patrol_status", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "subject": "patrol",
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
                    "question": "分析各巡检状态数量和完成情况",
                },
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="route_name",
            op="=",
            storage_value="route-id",
            display_value="华容－常宁高速公路",
            source="user_explicit",
            resolver="route_dictionary",
            raw_text="华容－常宁高速公路",
        )

        plan = analyze_single_turn(
            "华容－常宁高速公路在本月的巡检任务完成情况如何",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        spec = plan.nodes[0].spec
        self.assertEqual(spec.dimensions, ("patrol_status",))
        self.assertEqual(spec.metrics, ("count",))
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
    def test_patrol_route_question_uses_structured_patrol_plan(
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
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["route_name"],
                    "filters": [
                        {
                            "field": "create_time",
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
            field="create_time",
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
    def test_identifier_resolution_keeps_model_task_number_filter(
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
                            "field": "task_number",
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
            field="task_number",
            op="=",
            storage_value="XJJL202611040003",
            display_value="XJJL202611040003",
            source="user_explicit",
            resolver="identifier_dictionary:XJJL",
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
    def test_analysis_preserves_explicit_post_calc_spec(self, chat_json, _resolve):
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
        self.assertEqual(plan.nodes[1].spec.view, "top")
        self.assertEqual(plan.nodes[1].spec.metrics, ("count",))
        self.assertEqual(
            plan.nodes[1].spec.dimensions,
            ("route_name", "problem_type_name"),
        )

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
                    "action": "post_calc",
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
    def test_severity_question_does_not_rewrite_incomplete_model_plan(
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

        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(plan.nodes[0].spec.metrics, ("count",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_trend_question_accepts_structured_post_calculation(
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
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "判断月度数量趋势",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1"],
                },
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
