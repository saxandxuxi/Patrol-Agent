import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpecError
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.services.llm import LLMError


def invalid_distribution_draft(raw_value: str = "坑槽") -> dict:
    return {
        "final_goal": "按路线统计坑槽数量",
        "intent_family": "distribution",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "按路线统计坑槽数量",
                "subject": "disease",
                "view": "distribution",
                "metrics": ["count"],
                "dimensions": ["route_name"],
                "filters": [
                    {
                        "field": "problem_type_name",
                        "op": "=",
                        "raw_value": raw_value,
                    }
                ],
                "output_projection": ["route_name", "count"],
                "order_by": [
                    {
                        "field": "invented_dimension",
                        "direction": "desc",
                    }
                ],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            }
        ],
    }


class PlannerSelfRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_invalid_dimension_is_repaired_with_one_local_patch(
        self,
        chat_json,
        repair_chat_json,
        _audit,
    ):
        chat_json.return_value = invalid_distribution_draft()
        repair_chat_json.return_value = {
            "patches": [
                {
                    "op": "set",
                    "path": "/views/0/order_by",
                    "value": [
                        {
                            "field": "count",
                            "direction": "desc",
                        }
                    ],
                }
            ],
            "reason": "补上真实排序维度",
        }

        plan = analyze_single_turn(
            "按路线统计坑槽数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(repair_chat_json.call_count, 1)
        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertEqual(
            plan.nodes[0].spec.order_by,
            ({"field": "count", "direction": "desc"},),
        )
        self.assertEqual(
            plan.nodes[0].spec.filters[0].raw_text,
            "坑槽",
        )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_repair_patch_cannot_change_the_original_route_scope(
        self,
        chat_json,
        repair_chat_json,
        _audit,
    ):
        draft = invalid_distribution_draft()
        draft["views"][0]["filters"].insert(
            0,
            {
                "field": "route_name",
                "op": "=",
                "raw_value": "长沙－芷江高速公路",
            },
        )
        chat_json.return_value = draft
        repair_chat_json.return_value = {
            "patches": [
                {
                    "op": "set",
                    "path": "/views/0/filters/0/raw_value",
                    "value": "许昌-广州",
                }
            ],
            "reason": "错误地修改路线范围",
        }

        with self.assertRaisesRegex(QuerySpecError, "path is not allowed"):
            analyze_single_turn(
                "长沙－芷江高速公路按路线统计坑槽数量",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(repair_chat_json.call_count, 1)

    @patch("app.query.analysis.chat_json")
    @patch("app.query.planning_repair.controller.chat_json")
    def test_repair_model_rate_limit_is_not_wrapped_as_query_spec_error(
        self,
        repair_chat_json,
        chat_json,
    ):
        chat_json.return_value = invalid_distribution_draft()
        repair_chat_json.side_effect = LLMError(
            "HTTP 429",
            category="rate_limited",
            status_code=429,
            retryable=True,
        )

        with self.assertRaises(LLMError) as raised:
            analyze_single_turn(
                "按路线统计坑槽数量",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

        self.assertEqual(raised.exception.category, "rate_limited")
        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(repair_chat_json.call_count, 1)


if __name__ == "__main__":
    unittest.main()
