import unittest
from unittest.mock import patch

from app.query.analysis import (
    analyze_single_turn,
    structured_dependency_binding,
)
from app.query.capability_registry import CapabilityRegistry
from app.query.capability_registry.failures import CapabilityIssue
from app.query.contracts import QuerySpecError
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.planning_repair import (
    RepairIssue,
    RepairTicket,
    RepairPatchError,
    patch_signatures,
    request_repair,
)
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


def dependency_repair_draft(
    raw_value: str = "q1_top_route.route_name",
) -> dict:
    return {
        "final_goal": "先找主要路线，再统计该路线病害数量",
        "intent_family": "analysis",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1_top_route",
                "action": "query_data",
                "question": "按路线统计坑槽数量并取第一名",
                "subject": "disease",
                "view": "top_groups",
                "metrics": ["count"],
                "dimensions": ["route_name"],
                "filters": [],
                "output_projection": ["route_name", "count"],
                "order_by": [{"field": "count", "direction": "desc"}],
                "limit": 1,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "q2",
                "action": "query_data",
                "question": "统计该路线病害数量",
                "subject": "disease",
                "view": "count",
                "metrics": ["count"],
                "dimensions": [],
                "filters": [
                    {
                        "field": "route_name",
                        "op": "=",
                        "raw_value": raw_value,
                    }
                ],
                "output_projection": [],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
        ],
    }


class PlannerSelfRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_structured_dependency_requires_a_declared_node_output(self):
        binding = structured_dependency_binding(
            "q1.route_name",
            "route_name",
            "=",
            {"q1": {"route_name"}},
        )

        self.assertIsNotNone(binding)
        self.assertEqual(
            (
                binding.from_node,
                binding.from_field,
                binding.to_field,
                binding.op,
            ),
            ("q1", "route_name", "route_name", "="),
        )
        self.assertIsNone(
            structured_dependency_binding(
                "G65.west",
                "route_name",
                "=",
                {"q1": {"route_name"}},
            )
        )

    def test_depends_on_secret_protocol_is_no_longer_accepted(self):
        with self.assertRaisesRegex(
            QuerySpecError,
            "^invalid_dependency_value:",
        ):
            structured_dependency_binding(
                "DEPENDS_ON_q1_route_name",
                "route_name",
                "=",
                {"q1": {"route_name"}},
            )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_query_spec_dot_path_is_repaired_into_structured_input(
        self,
        chat_json,
        repair_chat_json,
        _audit,
        resolve_value,
    ):
        chat_json.return_value = dependency_repair_draft()
        repair_chat_json.return_value = {
            "patches": [
                {"op": "remove", "path": "/views/1/filters/0"},
                {
                    "op": "set",
                    "path": "/views/1/depends_on",
                    "value": ["q1_top_route"],
                },
                {
                    "op": "set",
                    "path": "/views/1/inputs",
                    "value": [
                        {
                            "from_node": "q1_top_route",
                            "from_field": "route_name",
                            "to_field": "route_name",
                            "op": "=",
                        }
                    ],
                },
            ],
            "reason": "把误写进过滤条件的依赖改成结构化输入绑定",
        }

        plan = analyze_single_turn(
            "坑槽最多的那条路有多少处病害",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        downstream = plan.nodes[1]
        self.assertEqual(downstream.depends_on, ("q1_top_route",))
        self.assertEqual(downstream.spec.filters, ())
        self.assertEqual(
            (
                downstream.spec.input_bindings[0].from_node,
                downstream.spec.input_bindings[0].from_field,
                downstream.spec.input_bindings[0].to_field,
            ),
            ("q1_top_route", "route_name", "route_name"),
        )
        resolve_value.assert_not_called()
        self.assertEqual(repair_chat_json.call_count, 1)

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

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_group_total_contract_uses_patch_repair(
        self,
        chat_json,
        repair_chat_json,
        audit_query_plan,
    ):
        chat_json.return_value = {
            "final_goal": "返回逐月巡査次数和总次数",
            "intent_family": "analysis",
            "answer_operation": "group_total",
            "unknown_terms": [],
            "views": [
                {
                    "id": "months",
                    "action": "query_data",
                    "question": "按月份统计巡査次数",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "comparison_depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "summary",
                    "action": "post_calc",
                    "question": "汇总分布和总次数",
                    "subject": "patrol",
                    "view": "count",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [
                        "analysis_only",
                        "preserve_user_scope",
                    ],
                    "depends_on": ["months"],
                    "comparison_depends_on": [],
                    "inputs": [],
                },
            ],
        }
        audit_query_plan.side_effect = [
            {
                "complete": False,
                "answer_operation": "group_total",
                "issues": [],
            },
            {
                "complete": True,
                "answer_operation": "group_total",
                "issues": [],
            },
        ]
        repair_chat_json.return_value = {
            "patches": [
                {
                    "op": "add",
                    "path": "/views/-",
                    "value": {
                        "id": "total",
                        "action": "query_data",
                        "question": "统计同一范围的巡査总次数",
                        "subject": "patrol",
                        "view": "count",
                        "metrics": ["count"],
                        "dimensions": [],
                        "filters": [],
                        "output_projection": [],
                        "order_by": [],
                        "limit": None,
                        "required_assertions": [
                            "preserve_user_scope",
                        ],
                        "depends_on": [],
                        "comparison_depends_on": [],
                        "inputs": [],
                    },
                },
                {
                    "op": "set",
                    "path": "/views/1/depends_on",
                    "value": ["months", "total"],
                },
            ],
            "reason": "补上同范围总体聚合并让汇总消费真实值",
        }

        plan = analyze_single_turn(
            "各月巡查次数分别是多少，总共多少次",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="patrol",
        )

        self.assertEqual(repair_chat_json.call_count, 1)
        self.assertEqual(
            [node.id for node in plan.nodes],
            ["months", "summary", "total"],
        )
        self.assertEqual(plan.nodes[-1].spec.dimensions, ())

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_unchanged_patch_gets_one_evidence_backed_repair_retry(
        self,
        chat_json,
        repair_chat_json,
        _audit,
    ):
        chat_json.return_value = invalid_distribution_draft()
        repair_chat_json.side_effect = [
            {
                "patches": [
                    {
                        "op": "set",
                        "path": "/views/0/order_by",
                        "value": [
                            {
                                "field": "invented_dimension",
                                "direction": "desc",
                            }
                        ],
                    }
                ]
            },
            {
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
                ]
            },
        ]

        plan = analyze_single_turn(
            "按路线统计坑槽数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.order_by[0]["field"], "count")
        self.assertEqual(repair_chat_json.call_count, 2)
        retry_prompt = str(repair_chat_json.call_args_list[1].args[0])
        self.assertIn("previous_repair_failure", retry_prompt)

    @patch("app.query.planning_repair.controller.chat_json")
    def test_self_dependency_patch_can_be_repaired_in_second_round(
        self,
        repair_chat_json,
    ):
        draft = invalid_distribution_draft()
        draft["views"][0]["depends_on"] = ["upstream"]
        ticket = RepairTicket(
            question="按路线统计坑槽数量",
            draft=draft,
            issue=RepairIssue(
                code="missing_dependency",
                path="/views/0/depends_on",
                reason="依赖关系不合法",
                allowed_paths=("/views/*/depends_on",),
            ),
        )
        repair_chat_json.side_effect = [
            {
                "patches": [
                    {
                        "op": "set",
                        "path": "/views/0/depends_on",
                        "value": ["q1"],
                    }
                ]
            },
            {
                "patches": [
                    {
                        "op": "set",
                        "path": "/views/0/depends_on",
                        "value": [],
                    }
                ]
            },
        ]

        repaired = request_repair(
            ticket,
            capabilities={},
            assist_evidence=(),
        )

        self.assertEqual(repaired["views"][0]["depends_on"], [])
        self.assertEqual(repair_chat_json.call_count, 2)
        retry_prompt = str(repair_chat_json.call_args_list[1].args[0])
        self.assertIn("self dependency", retry_prompt)

    @patch("app.query.planning_repair.controller.chat_json")
    def test_duplicate_patch_stops_without_another_model_call(
        self,
        repair_chat_json,
    ):
        patch = {
            "op": "set",
            "path": "/views/0/order_by",
            "value": [{"field": "count", "direction": "desc"}],
        }
        ticket = RepairTicket(
            question="按路线统计坑槽数量",
            draft=invalid_distribution_draft(),
            issue=RepairIssue(
                code="invalid_order_field",
                path="/views/0/order_by",
                reason="排序字段不存在",
                allowed_paths=("/views/*/order_by",),
            ),
            previous_signatures=frozenset(patch_signatures([patch])),
        )
        repair_chat_json.return_value = {"patches": [patch]}

        with self.assertRaisesRegex(RepairPatchError, "duplicate repair patch"):
            request_repair(
                ticket,
                capabilities={},
                assist_evidence=(),
            )

        self.assertEqual(repair_chat_json.call_count, 1)

    @patch("app.query.analysis._query_plan_compile_error")
    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_compile_issue_uses_patch_repair_instead_of_free_replan(
        self,
        chat_json,
        repair_chat_json,
        _audit,
        compile_error,
    ):
        draft = invalid_distribution_draft()
        draft["views"][0]["order_by"] = [
            {"field": "count", "direction": "desc"}
        ]
        chat_json.return_value = draft
        compile_error.side_effect = [
            CapabilityIssue(
                code="unknown_metric",
                original_value="fake_metric",
                reason="节点 q1 使用了未注册指标 fake_metric",
                allowed_values=("count", "sum_area"),
                node_id="q1",
            ),
            None,
        ]
        repair_chat_json.return_value = {
            "patches": [
                {
                    "op": "set",
                    "path": "/views/0/metrics",
                    "value": ["sum_area"],
                }
            ]
        }

        plan = analyze_single_turn(
            "按路线统计坑槽面积",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.metrics, ("sum_area",))
        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(repair_chat_json.call_count, 1)

    @patch("app.query.analysis._query_plan_compile_error")
    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_repeated_repair_evidence_stops_without_a_third_attempt(
        self,
        chat_json,
        repair_chat_json,
        _audit,
        compile_error,
    ):
        draft = invalid_distribution_draft()
        draft["views"][0]["order_by"] = [
            {"field": "count", "direction": "desc"}
        ]
        chat_json.return_value = draft
        compile_error.return_value = CapabilityIssue(
            code="unknown_metric",
            original_value="fake_metric",
            reason="节点 q1 使用了未注册指标 fake_metric",
            allowed_values=("count", "sum_area"),
            node_id="q1",
        )
        repair_chat_json.return_value = {
            "patches": [
                {
                    "op": "set",
                    "path": "/views/0/metrics",
                    "value": ["sum_area"],
                }
            ]
        }

        with self.assertRaisesRegex(QuerySpecError, "相同失败证据"):
            analyze_single_turn(
                "按路线统计坑槽面积",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(repair_chat_json.call_count, 1)

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.planning_repair.controller.chat_json")
    @patch("app.query.analysis.chat_json")
    def test_invalid_route_patch_does_not_drift_scope_on_repair_retry(
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
        repair_chat_json.side_effect = [
            {
                "patches": [
                    {
                        "op": "set",
                        "path": "/views/0/filters/0/raw_value",
                        "value": "许昌-广州",
                    }
                ]
            },
            {
                "patches": [
                    {
                        "op": "set",
                        "path": "/views/0/order_by",
                        "value": [{"field": "count", "direction": "desc"}],
                    }
                ]
            },
        ]

        plan = analyze_single_turn(
            "长沙－芷江高速公路按路线统计坑槽数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        route_filter = next(
            item
            for item in plan.nodes[0].spec.filters
            if item.field == "route_name"
        )
        self.assertEqual(route_filter.raw_text, "长沙－芷江高速公路")
        self.assertEqual(plan.nodes[0].spec.order_by[0]["field"], "count")
        self.assertEqual(repair_chat_json.call_count, 2)

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
        self.assertEqual(repair_chat_json.call_count, 2)

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
