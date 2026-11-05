import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.services.plan_contract import build_step_contract


class QueryPlanningTests(unittest.TestCase):
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
