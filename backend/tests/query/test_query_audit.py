import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.contracts import QuerySpecAmbiguity, QuerySpecError
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.services.llm import LLMError


class QueryAuditTests(unittest.TestCase):
    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_audit_receives_untrusted_top_n_evidence(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "路线内前四类病害",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count", "average_area"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        audit.return_value = {"complete": True, "issues": []}

        analyze_single_turn(
            "长沙绕城查一下前四类病害，数量和平均面积都给我。",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        evidence = audit.call_args.kwargs["assist_evidence"]
        top_n = next(
            item for item in evidence if item.rule_id == "top_n"
        )
        self.assertEqual(top_n.candidate_value, "4")
        self.assertFalse(top_n.trusted)

    @patch("app.query.analysis._analyze_once")
    def test_invalid_llm_json_gets_one_feedback_retry(self, analyze_once):
        from tests.query.factories import count_plan

        analyze_once.side_effect = [
            LLMError("模型没有返回合法 JSON"),
            count_plan(),
        ]

        plan = analyze_single_turn(
            "统计病害数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(analyze_once.call_count, 2)
        self.assertIn("合法 JSON", analyze_once.call_args_list[1].args[-1])

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_audit_patch_that_breaks_compiler_is_rejected(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "路线病害指标",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
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
                    "limit": 3,
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
                    "code": "invalid_view_shape",
                    "node_id": "q1",
                    "set": {
                        "metrics": ["count", "total_area", "average_area"],
                        "dimensions": ["route_name", "route_length_km"],
                    },
                }
            ],
        }

        plan = analyze_single_turn(
            "按路线统计数量和总面积，并关联路线长度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertIn("route_length_km", plan.nodes[0].spec.metrics)
        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertEqual(audit.call_count, 1)

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_noop_audit_issue_does_not_block_a_valid_plan(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "路线病害指标",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
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
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        audit.return_value = {
            "complete": False,
            "issues": [
                {
                    "code": "invalid_view_shape",
                    "node_id": "q1",
                    "reason": "路线长度不应作为病害指标",
                    "set": {"view": "distribution"},
                }
            ],
        }

        plan = analyze_single_turn(
            "按路线统计数量和总面积，并关联路线长度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertIn("route_length_km", plan.nodes[0].spec.metrics)
        self.assertEqual(audit.call_count, 1)

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_audit_aggregate_view_alias_is_normalized_to_distribution(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "设备路线任务次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name"],
                    "order_by": [],
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
                        "code": "invalid_view_shape",
                        "node_id": "q1",
                        "set": {
                            "view": "aggregate",
                            "dimensions": ["device_id", "route_name"],
                            "metrics": [{"field": "task_id", "agg": "count"}],
                        },
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "2026年11月每台巡检设备分别完成了哪些路线、各多少次任务？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        spec = plan.nodes[0].spec
        self.assertEqual(spec.view, "distribution")
        self.assertEqual(spec.metrics, ("count",))
        self.assertEqual(spec.dimensions, ("equipment_id", "route_name"))

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_audit_group_view_alias_is_normalized_before_validation(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "设备路线任务次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["equipment_id", "route_name"],
                    "filters": [],
                    "output_projection": ["equipment_id", "route_name", "count"],
                    "order_by": [],
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
                        "code": "invalid_view_shape",
                        "node_id": "q1",
                        "set": {"view": "group"},
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "每台巡检设备在各条路线上分别完成多少次任务",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(plan.nodes[0].spec.view, "distribution")

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis.chat_json")
    def test_audit_metric_patch_is_normalized_to_query_contract(
        self,
        chat_json,
        audit,
    ):
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
                    "dimensions": ["equipment_id"],
                    "filters": [],
                    "output_projection": ["equipment_id", "count"],
                    "order_by": [],
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
                        "code": "missing_metric",
                        "node_id": "q1",
                        "set": {
                            "metrics": [
                                {"field": "task_id", "agg": "count"}
                            ]
                        },
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "每台巡检设备分别完成多少次任务",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(plan.nodes[0].spec.metrics, ("count",))

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
    @patch("app.query.analysis._analyze_once")
    def test_ambiguous_scope_is_fed_back_before_requesting_user(
        self,
        analyze_once,
        audit,
    ):
        """审计口径问题必须先交给规划器重排，不能首轮直接拒绝用户。"""

        from tests.query.factories import count_plan

        analyze_once.side_effect = [count_plan(), count_plan()]
        audit.side_effect = [
            {
                "complete": False,
                "issues": [
                    {
                        "code": "ambiguous_scope",
                        "node_id": "q1",
                        "reason": "计划没有在子问题中写明用户已经给出的路线和桩号范围",
                        "repair_hint": "保留原范围并补全节点问题，不要新增时间条件",
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "长芷高速K400到K410有没有网状裂缝？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(analyze_once.call_count, 2)
        feedback = analyze_once.call_args_list[1].args[-1]
        self.assertIn("ambiguous_scope", feedback)
        self.assertIn("不要新增时间条件", feedback)

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis._analyze_once")
    def test_non_ambiguous_audit_failure_is_not_reported_as_missing_scope(
        self,
        analyze_once,
        audit,
    ):
        """审计说计划缺结构时，不能伪装成让用户补时间或批次的歧义提示。"""

        from tests.query.factories import count_plan

        analyze_once.side_effect = [count_plan(), count_plan(), count_plan()]
        audit.return_value = {
            "complete": False,
            "issues": [
                {
                    "code": "missing_metric",
                    "node_id": "",
                    "reason": "计划缺少覆盖状态节点",
                    "repair_hint": "补充覆盖状态查询",
                    "set": {},
                }
            ],
        }

        with self.assertRaises(QuerySpecError) as raised:
            analyze_single_turn(
                "这个桩号范围是否超出数据覆盖？",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
            )

        self.assertIn("规划审计", str(raised.exception))
        self.assertNotIn("请补充具体时间范围", str(raised.exception))

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
    def test_post_calc_upstream_missing_metric_patch_is_not_ignored(
        self,
        chat_json,
        audit,
    ):
        chat_json.return_value = {
            "final_goal": "路线健康比较",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count", "total_area"],
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
        audit.side_effect = [
            {
                "complete": False,
                "issues": [
                    {
                        "code": "missing_metric",
                        "node_id": "q1",
                        "reason": "密度计算需要路线长度",
                        "set": {
                            "metrics": [
                                "count",
                                "total_area",
                                "route_length_km",
                            ],
                            "output_projection": [
                                "route_name",
                                "count",
                                "total_area",
                                "route_length_km",
                            ],
                        },
                    }
                ],
            },
            {"complete": True, "issues": []},
        ]

        plan = analyze_single_turn(
            "比较路线病害密度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertIn("route_length_km", plan.nodes[0].spec.metrics)
        self.assertEqual(audit.call_count, 2)

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
                    "limit": 3,
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
