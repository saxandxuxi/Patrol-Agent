import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.analysis_contracts import complete_analysis_answer
from app.query.contracts import (
    PlanNode,
    QueryPlan,
    QuerySpec,
    ValueBinding,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView


class AnalysisContractTests(unittest.TestCase):
    def test_trend_without_analysis_intent_does_not_add_route_length(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="trend",
            metrics=("total_area",),
            dimensions=("collection_time_day",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="route-51ca",
                    display_value="长沙－芷江高速公路",
                    source="user_explicit",
                    resolver="route_dictionary",
                ),
            ),
            output_projection=("collection_time_day", "total_area"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        plan = QueryPlan(
            question="长沙－芷江高速公路2026年11月病害总面积的每日变化趋势",
            nodes=(PlanNode(id="q1", spec=spec),),
        )

        completed = complete_analysis_answer(plan, "trend")

        self.assertEqual(completed.nodes, plan.nodes)
        self.assertFalse(
            any(
                node.spec.metrics == ("route_length_km",)
                for node in completed.nodes
            )
        )

    @patch("app.query.analysis.chat_json")
    def test_independent_analysis_intent_adds_terminal_analysis(self, chat_json):
        chat_json.return_value = {
            "final_goal": "比较道路病害情况并给出结论",
            "intent_family": "data",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计病害数量和总面积",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count", "total_area"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "哪个道路的泛油问题更严重？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
        )

        self.assertEqual(plan.response_mode, "analysis")
        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertEqual(plan.nodes[-1].depends_on, ("q1",))
        self.assertIn(
            "analysis_only",
            plan.nodes[-1].spec.required_assertions,
        )
        self.assertIn(
            "multi_metric_comparison",
            plan.nodes[-1].spec.required_assertions,
        )

    @patch("app.query.analysis.chat_json")
    def test_non_route_single_dimension_analysis_uses_deterministic_comparison(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "综合判断损坏程度分布",
            "intent_family": "data",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按损坏程度统计病害数量和总面积",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["damage_type"],
                    "filters": [],
                    "output_projection": [
                        "damage_type",
                        "count",
                        "total_area",
                    ],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "当前病害的严重程度分布怎么样？请按损坏程度给出数量、面积和综合判断。",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
        )

        self.assertEqual(plan.response_mode, "analysis")
        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertIn(
            "analysis_only",
            plan.nodes[-1].spec.required_assertions,
        )
        self.assertIn(
            "multi_metric_comparison",
            plan.nodes[-1].spec.required_assertions,
        )

    @patch("app.query.analysis.chat_json")
    def test_planner_analysis_node_inherits_single_dimension_comparison_contract(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "综合判断损坏程度分布",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按损坏程度统计病害数量和总面积",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["damage_type"],
                    "filters": [],
                    "output_projection": [
                        "damage_type",
                        "count",
                        "total_area",
                    ],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "p1",
                    "action": "post_calc",
                    "question": "综合判断损坏程度分布",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["analysis_only"],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }

        plan = analyze_single_turn(
            "当前病害的严重程度分布怎么样？请按损坏程度给出数量、面积和综合判断。",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
        )

        self.assertIn(
            "multi_metric_comparison",
            plan.nodes[-1].spec.required_assertions,
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_single_route_trend_carries_route_length(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "判断长沙至芷江高速病害数量趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按月统计该路线病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "长沙－芷江高速公路",
                        }
                    ],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="route_name",
            op="=",
            storage_value="route-51ca",
            display_value="长沙－芷江高速公路",
            source="user_explicit",
            resolver="route_dictionary",
            raw_text="长沙－芷江高速公路",
        )

        plan = analyze_single_turn(
            "长沙－芷江高速公路最近三个月的病害数量变化趋势如何？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
        )

        trend = plan.nodes[0]
        context = next(
            node
            for node in plan.nodes
            if node.node_type == "query_data" and node.id != trend.id
        )
        analysis = plan.nodes[-1]

        self.assertNotIn("route_length_km", trend.spec.metrics)
        self.assertEqual(context.spec.metrics, ("route_length_km",))
        self.assertEqual(context.spec.view, "distribution")
        self.assertEqual(
            context.spec.dimensions,
            ("route_name", "routes_id"),
        )
        self.assertIn("analysis_context", context.spec.required_assertions)
        self.assertIn(context.id, analysis.depends_on)

    @patch(
        "app.query.analysis.preflight_spec_mdl",
        return_value="SELECT 1",
    )
    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_audit_cannot_strip_single_route_trend_length(
        self,
        chat_json,
        resolve_value,
        audit,
        _preflight_spec_mdl,
    ):
        chat_json.return_value = {
            "final_goal": "判断长沙至芷江高速病害数量趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按月统计该路线病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "长沙－芷江高速公路",
                        }
                    ],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="route_name",
            op="=",
            storage_value="route-51ca",
            display_value="长沙－芷江高速公路",
            source="user_explicit",
            resolver="route_dictionary",
            raw_text="长沙－芷江高速公路",
        )
        audit.return_value = {
            "complete": False,
            "issues": [
                {
                    "code": "unrequested_metric",
                    "node_id": "q1",
                    "set": {
                        "metrics": ["count"],
                        "output_projection": ["month", "count"],
                    },
                }
            ],
        }

        plan = analyze_single_turn(
            "长沙－芷江高速公路最近三个月的病害数量变化趋势如何？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
            audit=True,
        )

        trend = plan.nodes[0]
        context = next(
            node
            for node in plan.nodes
            if node.node_type == "query_data" and node.id != trend.id
        )

        self.assertNotIn("route_length_km", trend.spec.metrics)
        self.assertEqual(context.spec.metrics, ("route_length_km",))
        self.assertEqual(context.spec.view, "distribution")
        self.assertEqual(
            context.spec.dimensions,
            ("route_name", "routes_id"),
        )

    @patch("app.query.analysis.chat_json")
    def test_route_analysis_comparison_carries_route_length(self, chat_json):
        chat_json.return_value = {
            "final_goal": "比较道路病害情况并给出结论",
            "intent_family": "data",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计病害数量和总面积",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count", "total_area"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "哪个道路的泛油问题更严重？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
        )

        grouped = plan.nodes[0].spec
        conclusion = plan.nodes[-1].spec
        self.assertIn("route_length_km", grouped.metrics)
        self.assertIn("route_length_km", grouped.output_projection)
        self.assertIn(
            "multi_metric_comparison",
            conclusion.required_assertions,
        )

    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_analysis_comparison_repairs_single_candidate_limit(
        self,
        chat_json,
        repair_chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "比较道路病害情况并给出结论",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计病害数量和总面积",
                    "subject": "disease",
                    "view": "top_groups",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count", "total_area"],
                    "order_by": [{"field": "total_area", "direction": "desc"}],
                    "limit": 1,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "综合比较路线病害情况",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [
                        "analysis_only",
                        "multi_metric_comparison",
                    ],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }

        repair_chat_json.return_value = {
            "patches": [
                {
                    "op": "set",
                    "path": "/views/0/limit",
                    "value": None,
                }
            ]
        }

        plan = analyze_single_turn(
            "哪个道路的泛油问题更严重？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            intent="analysis",
        )

        self.assertIsNone(plan.nodes[0].spec.limit)
        self.assertEqual(repair_chat_json.call_count, 1)


if __name__ == "__main__":
    unittest.main()
