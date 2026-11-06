import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.contracts import QuerySpecAmbiguity
from app.query.fields import FieldReachSet, PhysicalSchemaView


class QueryAuditTests(unittest.TestCase):
    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis._analyze_once")
    def test_planning_parse_error_gets_one_feedback_retry(
        self,
        analyze_once,
        audit,
    ):
        from tests.query.factories import count_plan

        analyze_once.side_effect = [
            QuerySpecAmbiguity("查询条件缺少可执行值"),
            count_plan(),
        ]
        audit.return_value = {"complete": True, "issues": []}

        plan = analyze_single_turn(
            "需要重试的问题",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(analyze_once.call_count, 2)
        self.assertIn(
            "解析失败",
            analyze_once.call_args_list[1].args[-1],
        )

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_unresolved_plan_is_reported_as_ambiguous_scope(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "最近任务的最高里程",
            "intent_family": "top",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "top",
                    "metrics": ["patrol_mileage"],
                    "dimensions": ["id", "patrol_mileage"],
                    "filters": [],
                    "output_projection": ["id", "patrol_mileage"],
                    "order_by": [{"field": "patrol_mileage", "direction": "desc"}],
                    "limit": 1,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        audit.return_value = {
            "complete": False,
            "issues": [
                {
                    "code": "ambiguous_scope",
                    "node_id": "q1",
                    "reason": "“最近”没有明确具体时间范围或巡检批次",
                    "repair_hint": "",
                }
            ],
        }

        with self.assertRaises(QuerySpecAmbiguity) as raised:
            analyze_single_turn(
                "最近的巡检任务中，巡检完成里程最高的是哪一次？",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
            )

        self.assertIn("时间范围", str(raised.exception))
        self.assertIn("巡检批次", str(raised.exception))
        self.assertNotIn("\"code\"", str(raised.exception))

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_post_calc_validation_is_not_duplicated_by_plan_audit(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "比较结果",
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
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }
        audit.return_value = {
            "complete": False,
            "issues": [
                {
                    "code": "missing_metric",
                    "node_id": "q2",
                    "reason": "post_calc 未声明指标",
                },
                {
                    "code": "missing_metric",
                    "node_id": "q1",
                    "reason": "最终派生指标由 post_calc 计算",
                },
                {
                    "code": "missing_metric",
                    "node_id": "",
                    "reason": "最终输出缺少比较比例",
                },
            ],
        }

        plan = analyze_single_turn(
            "比较各路线结果",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_structured_audit_patch_is_applied_without_changing_scope(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "Top 5",
            "intent_family": "top",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["problem_type_name"],
                    "filters": [],
                    "output_projection": ["problem_type_name", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        audit.side_effect = [
            {
                "complete": False,
                "issues": [
                    {
                        "code": "lost_filter",
                        "node_id": "q1",
                        "reason": "缺少 Top 5 限制",
                        "set": {"limit": 5},
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "最常见的5种病害是什么",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(plan.nodes[0].spec.limit, 5)
        self.assertEqual(chat_json.call_count, 1)

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_incomplete_plan_is_repaired_with_audit_feedback(
        self,
        chat_json,
        audit,
    ):
        base_view = {
            "id": "q1",
            "action": "query_data",
            "subject": "patrol",
            "view": "distribution",
            "metrics": ["count"],
            "filters": [],
            "order_by": [],
            "limit": None,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": [],
            "inputs": [],
        }
        chat_json.side_effect = [
            {
                "final_goal": "设备路线任务数",
                "intent_family": "distribution",
                "views": [
                    {
                        **base_view,
                        "dimensions": ["equipment_id"],
                        "output_projection": ["equipment_id", "count"],
                        "question": "按设备统计任务数",
                    }
                ],
            },
            {
                "final_goal": "设备路线任务数",
                "intent_family": "distribution",
                "views": [
                    {
                        **base_view,
                        "dimensions": ["equipment_id", "route_name"],
                        "output_projection": ["equipment_id", "route_name", "count"],
                        "question": "按设备和路线统计任务数",
                    }
                ],
            },
        ]
        audit.side_effect = [
            {
                "complete": False,
                "issues": [
                    {
                        "code": "missing_dimension",
                        "reason": "缺少路线维度",
                        "repair_hint": "增加 route_name",
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "统计每台巡检设备在各条路线上的任务次数",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(
            plan.nodes[0].spec.dimensions,
            ("equipment_id", "route_name"),
        )
        self.assertEqual(chat_json.call_count, 2)
        second_messages = chat_json.call_args_list[1].args[0]
        self.assertIn("增加 route_name", second_messages[-1]["content"])
