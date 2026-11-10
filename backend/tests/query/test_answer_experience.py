import unittest
from unittest.mock import patch

from app.calculations.evidence import compact_table_evidence
from app.calculations.post_calc import execute_post_calc
from app.query.analysis import _canonical_dimensions, analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import ExecutionObservation, QuerySpec
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.scope_facts import PARTIAL
from app.query.service import _answer_text, assemble_answer


class AnswerExperienceTests(unittest.TestCase):
    @patch("app.calculations.domain.analysis.chat")
    def test_multi_metric_assertion_uses_all_available_dimensions(self, chat):
        chat.return_value = (
            "综合病害数量、总面积、平均面积、病害长度和病害密度，"
            "A 路线的综合负担最低；依据是 A 的 5 项代理指标均低于 B。"
        )
        payload = execute_post_calc(
            {
                "id": "p1",
                "question": "综合判断哪条路线健康状况最好",
                "depends_on": [1],
                "metrics": ["disease_density"],
                "required_assertions": [
                    "lower_is_better",
                    "multi_metric_comparison",
                    "analysis_only",
                ],
            },
            {
                1: {
                    "columns": [
                        "路线名称",
                        "数量",
                        "总面积",
                        "平均面积",
                        "总长度",
                        "路线长度_km",
                    ],
                    "rows": [
                        {
                            "路线名称": "A",
                            "数量": "5",
                            "总面积": "20",
                            "平均面积": "4",
                            "总长度": "10",
                            "路线长度_km": "10",
                        },
                        {
                            "路线名称": "B",
                            "数量": "8",
                            "总面积": "40",
                            "平均面积": "5",
                            "总长度": "16",
                            "路线长度_km": "20",
                        },
                    ],
                    "spec": {
                        "metrics": [
                            "count",
                            "sum_area",
                            "avg_area",
                            "sum_effective_disease_length",
                            "route_length_km",
                        ],
                        "dimensions": ["route_name"],
                    },
                    "field_labels": {
                        "count": "数量",
                        "sum_area": "总面积",
                        "avg_area": "平均面积",
                        "sum_effective_disease_length": "总长度",
                        "route_length_km": "路线长度_km",
                        "route_name": "路线名称",
                    },
                }
            },
        )

        chat.assert_called_once()
        self.assertTrue(payload["accepted"])
        self.assertIn("综合", payload["reply"])
        self.assertIn("病害密度", payload["reply"])

    @patch("app.calculations.executor.analyze_evidence")
    def test_large_calculation_sends_preview_and_full_compact_evidence(
        self,
        analyze,
    ):
        analyze.return_value = {
            "reply": "ok",
            "source": "calc",
            "engine": "post_calc",
            "accepted": True,
        }
        rows = [
            {
                "路线名称": f"路线{index}",
                "数量": index + 1,
                "路线长度": 10,
            }
            for index in range(15)
        ]

        execute_post_calc(
            {
                "question": "计算密度",
                "depends_on": ["q1"],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": "divide",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q1", "field": "数量"},
                        },
                        {
                            "name": "denominator",
                            "source": {"node": "q1", "field": "路线长度"},
                        },
                    ],
                    "output": {"name": "密度", "unit": "处/km"},
                    "precision": 4,
                },
            },
            {
                "q1": {
                    "rows": rows,
                    "field_labels": {
                        "route_name": "路线名称",
                        "count": "数量",
                        "route_length_km": "路线长度",
                    },
                }
            },
        )

        calculation_dependency = analyze.call_args.args[1][-1]
        self.assertEqual(calculation_dependency["row_count"], 15)
        self.assertEqual(len(calculation_dependency["rows"]), 10)
        self.assertEqual(
            calculation_dependency["compact"]["metrics"]["密度"]["max"]["group"],
            "路线14",
        )

    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=AssertionError("LLM should not be called"),
    )
    def test_structured_trend_uses_sequence_and_metric_specs(self, _chat):
        payload = execute_post_calc(
            {
                "id": "q3",
                "question": "计算最近巡查的病害数量变化",
                "depends_on": [1, 2],
                "view": "trend",
                "calculation": {
                    "tool": "calculate_series",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "labels",
                            "source": {"node": "1", "field": "patrol_number"},
                        },
                        {
                            "name": "values",
                            "source": {"node": "2", "field": "count"},
                        },
                    ],
                    "precision": 6,
                },
            },
            {
                1: {
                    "question": "最近3次巡查",
                    "spec": {
                        "view": "list",
                        "metrics": [],
                        "dimensions": [],
                        "output_projection": [
                            "patrol_number",
                            "end_date",
                        ],
                        "order_by": [
                            {"field": "end_date", "direction": "desc"}
                        ],
                    },
                    "field_labels": {
                        "patrol_number": "任务编号",
                        "end_date": "结束时间",
                    },
                    "columns": ["任务编号", "结束时间"],
                    "rows": [
                        {
                            "任务编号": "P3",
                            "结束时间": "2026-11-03",
                        },
                        {
                            "任务编号": "P2",
                            "结束时间": "2026-11-02",
                        },
                        {
                            "任务编号": "P1",
                            "结束时间": "2026-11-01",
                        },
                    ],
                },
                2: {
                    "question": "按巡查批次统计坑槽数量",
                    "spec": {
                        "view": "distribution",
                        "metrics": ["count"],
                        "dimensions": ["patrol_number"],
                        "output_projection": ["patrol_number", "count"],
                        "order_by": [],
                    },
                    "field_labels": {
                        "patrol_number": "任务编号",
                        "count": "数量",
                    },
                    "columns": ["任务编号", "数量"],
                    "rows": [
                        {"任务编号": "P1", "数量": "8"},
                        {"任务编号": "P2", "数量": "3"},
                        {"任务编号": "P3", "数量": "5"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("=8", payload["reply"])
        self.assertIn("=3", payload["reply"])
        self.assertIn("=5", payload["reply"])
        self.assertIn("-5，+2", payload["reply"])
        self.assertEqual(
            [row["value"] for row in payload["rows"]],
            ["8", "3", "5"],
        )

    def test_scalar_answer_keeps_multiple_metrics(self):
        from app.query.contracts import ExecutionObservation
        from app.query.service import _answer_text

        spec = QuerySpec(
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
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("数量", "总面积"),
            preview_rows=({"数量": "10", "总面积": "25.5"},),
            result_rows=({"数量": "10", "总面积": "25.5"},),
            rows_fetched=1,
        )

        answer = _answer_text(spec, observation)

        self.assertIn("数量 10处", answer)
        self.assertIn("总面积 25.5㎡", answer)

    def test_empty_detail_answer_says_no_matching_records(self):
        from app.query.contracts import ExecutionObservation
        from app.query.service import _answer_text

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="list",
            metrics=(),
            dimensions=("pile_number",),
            filters=(),
            output_projection=("pile_number",),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT pile_number FROM ai_problem WHERE 1 = 0",
            columns=("桩号",),
            row_count=0,
            preview_rows=(),
            result_rows=(),
            rows_fetched=0,
        )

        answer = _answer_text(spec, observation)

        self.assertIn("没有匹配记录", answer)
        self.assertNotIn("明细返回 0 条", answer)

    def test_empty_detail_section_points_to_the_filters_not_the_workbench(self):
        from app.query.service import _data_answer_text

        answer = _data_answer_text(
            {
                "title": "包茂高速10月网状裂缝明细",
                "type": "query_data",
                "view": "list",
                "status": "accepted",
                "rows_fetched": 0,
                "rows": [],
                "preview_rows": [],
                "spec": {
                    "subject": "disease",
                    "view": "list",
                },
            }
        )

        self.assertIn("没有匹配记录", answer)
        self.assertNotIn("结果工作台", answer)

    def test_route_length_uses_registered_kilometre_unit(self):
        from app.query.contracts import ExecutionObservation
        from app.query.service import _answer_text

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("route_length_km",),
            dimensions=(),
            filters=(),
            output_projection=("route_length_km",),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 540.299",
            columns=("路线长度",),
            preview_rows=({"路线长度": "540.299"},),
            result_rows=({"路线长度": "540.299"},),
            rows_fetched=1,
        )

        answer = _answer_text(spec, observation)

        self.assertIn("540.299公里", answer)
        self.assertNotIn("540.299米", answer)

    def test_explicit_pile_between_discloses_closed_boundaries(self):
        from app.query.contracts import ExecutionObservation, ValueBinding
        from app.query.service import _answer_text

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="pile_number",
                    op="between",
                    storage_value=(200000, 210000),
                    display_value="K200,K210",
                    source="user_explicit",
                    resolver="pile_range",
                    raw_text="K200到K210",
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*)",
            columns=("病害数量",),
            preview_rows=({"病害数量": "125"},),
            result_rows=({"病害数量": "125"},),
            rows_fetched=1,
        )

        answer = _answer_text(spec, observation)

        self.assertIn("K200", answer)
        self.assertIn("K210", answer)
        self.assertIn("包含", answer)
        self.assertNotIn("。；", answer)

    def test_group_digest_uses_spec_subject_when_section_lacks_top_level_subject(self):
        from app.query.service import _data_answer_text

        answer = _data_answer_text(
            {
                "type": "data",
                "view": "top_groups",
                "status": "accepted",
                "rows_fetched": 1,
                "rows": [{"病害类型": "条状修补", "数量": "11129"}],
                "preview_rows": [{"病害类型": "条状修补", "数量": "11129"}],
                "spec": {
                    "subject": "disease",
                    "view": "top_groups",
                },
            }
        )

        self.assertIn("数量 11129处", answer)
        self.assertNotIn("11129条", answer)

    def test_group_digest_uses_units_registered_for_serialized_spec(self):
        from app.query.service import _data_answer_text

        answer = _data_answer_text(
            {
                "type": "data",
                "view": "top_groups",
                "status": "accepted",
                "rows_fetched": 1,
                "rows": [
                    {
                        "路线名称": "平江－洞口高速公路",
                        "数量": "9136",
                        "路线长度": "319.694",
                    }
                ],
                "preview_rows": [
                    {
                        "路线名称": "平江－洞口高速公路",
                        "数量": "9136",
                        "路线长度": "319.694",
                    }
                ],
                "spec": {
                    "kind": "mysql",
                    "subject": "disease",
                    "view": "top_groups",
                    "metrics": ["count", "route_length_km"],
                    "output_projection": [
                        "route_name",
                        "count",
                        "route_length_km",
                    ],
                },
            }
        )

        self.assertIn("319.694公里", answer)
        self.assertNotIn("319.694米", answer)

    def test_top_groups_small_result_lists_every_requested_group(self):
        from app.query.service import _data_answer_text

        rows = [
            {"病害类型": "条状修补", "数量": "206", "平均面积": "0.379757"},
            {"病害类型": "绿化缺失", "数量": "43", "平均面积": "55.843721"},
            {"病害类型": "抛洒物", "数量": "43", "平均面积": "0.04"},
            {"病害类型": "路肩垃圾", "数量": "17", "平均面积": "0.054118"},
        ]
        answer = _data_answer_text(
            {
                "type": "data",
                "view": "top_groups",
                "status": "accepted",
                "rows_fetched": 4,
                "rows": rows,
                "preview_rows": rows,
                "spec": {
                    "subject": "disease",
                    "view": "top_groups",
                },
            }
        )

        for row in rows:
            self.assertIn(row["病害类型"], answer)

    def test_top_groups_explains_when_fewer_groups_than_requested(self):
        from app.query.service import _data_answer_text

        answer = _data_answer_text(
            {
                "type": "data",
                "view": "top_groups",
                "status": "accepted",
                "rows_fetched": 1,
                "rows": [{"桩号分段": "K1+000~K2+000", "数量": "1"}],
                "preview_rows": [{"桩号分段": "K1+000~K2+000", "数量": "1"}],
                "spec": {
                    "subject": "disease",
                    "view": "top_groups",
                    "limit": 5,
                },
            }
        )

        self.assertIn("仅命中 1 个分组", answer)
        self.assertIn("少于请求的 Top 5", answer)

    def test_top_groups_explains_explicit_requested_group_without_rows(self):
        from app.query.service import _data_answer_text

        answer = _data_answer_text(
            {
                "type": "data",
                "view": "top_groups",
                "status": "accepted",
                "rows_fetched": 1,
                "rows": [
                    {
                        "上下行方向": "下行",
                        "病害类型": "条状修补",
                        "数量": "1396",
                    }
                ],
                "preview_rows": [
                    {
                        "上下行方向": "下行",
                        "病害类型": "条状修补",
                        "数量": "1396",
                    }
                ],
                "missing_requested_groups": ["上下行方向 上行"],
                "spec": {
                    "subject": "disease",
                    "view": "top_groups",
                    "limit": 1,
                },
            }
        )

        self.assertIn("现有覆盖范围内没有匹配记录", answer)
        self.assertIn("上下行方向 上行", answer)

    def test_partial_coverage_zero_rows_reports_observed_overlap_and_unknown_remainder(
        self,
    ):
        from app.query.service import assemble_answer

        answer = assemble_answer(
            [
                {
                    "type": "query_data",
                    "view": "distribution",
                    "status": "accepted",
                    "rows_fetched": 0,
                    "rows": [],
                    "preview_rows": [],
                    "coverage_verdict": PARTIAL,
                    "coverage_detail": (
                        "包头-茂名现有病害记录的采集时间在 "
                        "2026-10-28 16:36 至 2026-11-04 09:43"
                    ),
                    "spec": {
                        "subject": "disease",
                        "view": "distribution",
                    },
                }
            ],
            response_mode="data",
        )

        self.assertIn("在查询范围内没有匹配记录（0 行）", answer.text)
        self.assertIn(
            "在现有记录覆盖到的重叠区间内未观察到匹配记录",
            answer.text,
        )
        self.assertIn("2026-11-04", answer.text)

    def test_scalar_answer_does_not_unit_label_category_or_expose_null(self):
        from app.query.contracts import ExecutionObservation
        from app.query.service import _answer_text

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count", "total_area"),
            dimensions=(),
            filters=(),
            output_projection=("problem_type_name", "count", "total_area"),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("病害类型", "处数", "总面积"),
            preview_rows=(
                {"病害类型": "坑洞", "处数": "0", "总面积": "NULL"},
            ),
            result_rows=(
                {"病害类型": "坑洞", "处数": "0", "总面积": "NULL"},
            ),
            rows_fetched=1,
        )

        answer = _answer_text(spec, observation)

        self.assertIn("病害类型 坑洞", answer)
        self.assertNotIn("坑洞处", answer)
        self.assertIn("处数 0处", answer)
        self.assertIn("总面积 无有效数值", answer)
        self.assertNotIn("NULL", answer)

    def test_group_digest_trims_decimal_noise_and_applies_metric_units(self):
        answer = assemble_answer(
            [
                {
                    "node_id": "q1",
                    "title": "q1. 按路线统计",
                    "type": "query_data",
                    "view": "top_groups",
                    "status": "accepted",
                    "answer": "",
                    "subject": "disease",
                    "rows": [
                        {
                            "路线名称": "平江－洞口高速公路",
                            "数量": "577",
                            "总面积": "1085.6200000000",
                        }
                    ],
                    "preview_rows": [
                        {
                            "路线名称": "平江－洞口高速公路",
                            "数量": "577",
                            "总面积": "1085.6200000000",
                        }
                    ],
                    "rows_fetched": 1,
                    "total_count": 1,
                    "truncated": False,
                }
            ],
            response_mode="data",
        )

        self.assertIn("数量 577处", answer.text)
        self.assertIn("总面积 1085.62㎡", answer.text)
        self.assertNotIn("1085.6200000000", answer.text)

    def test_patrol_mileage_dynamic_column_keeps_km_unit(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("patrol_mileage",),
            dimensions=("equipment_id",),
            filters=(),
            output_projection=("equipment_id", "patrol_mileage"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="每台巡检设备累计巡检里程",
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT equipment_id, SUM(patrol_mileage) / 1000",
            columns=("巡检设备", "巡检里程合计"),
            preview_rows=(
                {"巡检设备": "1420324267072", "巡检里程合计": "798.55"},
            ),
            result_rows=(
                {"巡检设备": "1420324267072", "巡检里程合计": "798.55"},
            ),
            rows_fetched=1,
        )

        answer = _answer_text(
            spec,
            observation,
            registry=CapabilityRegistry.load(),
        )

        self.assertIn("巡检里程合计 798.55公里", answer)

    @patch("app.query.analysis.chat_json")
    def test_analysis_intent_adds_terminal_analysis_node(self, chat_json):
        chat_json.return_value = {
            "final_goal": "判断哪条路线健康状况最好并说明维度",
            "intent_family": "analysis",
            "unknown_terms": ["健康状况"],
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计病害数量、面积和长度",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": [
                        "count",
                        "total_area",
                        "average_area",
                        "route_length_km",
                        "sum_effective_disease_length",
                    ],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": [
                        "route_name",
                        "count",
                        "sum_area",
                        "sum_length",
                    ],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "比较路线指标并判断健康状况",
                    "subject": "disease",
                    "view": "derived_metrics",
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
            "哪条路的健康状况最好？从哪几个维度判断？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.response_mode, "analysis")
        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertEqual(plan.nodes[-1].spec.view, "data")
        self.assertEqual(plan.nodes[-1].depends_on, ("q1",))
        self.assertIn("total_area", plan.nodes[0].spec.metrics)
        self.assertIn("average_area", plan.nodes[0].spec.metrics)
        self.assertIn("route_length_km", plan.nodes[0].spec.metrics)
        self.assertTrue(
            {
                "sum_disease_length",
                "sum_effective_disease_length",
            }.intersection(plan.nodes[0].spec.metrics)
        )

    def test_data_distribution_answer_points_to_workbench(self):
        answer = assemble_answer(
            [
                {
                    "node_id": "q1",
                    "title": "q1. 按路线和病害类型统计",
                    "type": "query_data",
                    "view": "distribution",
                    "status": "accepted",
                    "answer": "路线名称 A，病害类型 条状修补，数量 1",
                    "rows_fetched": 237,
                    "total_count": 237,
                    "truncated": False,
                }
            ],
            response_mode="data",
        )

        self.assertIn("237", answer.text)
        self.assertIn("工作台", answer.text)
        self.assertNotIn("路线名称 A", answer.text)

    def test_single_row_group_lists_everything_it_has(self):
        """分组只有 1 行时要说明“已全部列出”，
        否则用户分不清是完整清单还是只看了前几名。"""

        answer = assemble_answer(
            [
                {
                    "node_id": "q1",
                    "title": "q1. 按病害类型分组统计",
                    "type": "query_data",
                    "view": "distribution",
                    "status": "accepted",
                    "answer": "",
                    "rows": [{"病害类型": "防眩板缺损", "数量": "1"}],
                    "preview_rows": [{"病害类型": "防眩板缺损", "数量": "1"}],
                    "rows_fetched": 1,
                    "total_count": 1,
                    "truncated": False,
                }
            ],
            response_mode="data",
        )

        self.assertIn("已全部列出", answer.text)
        self.assertIn("防眩板缺损", answer.text)
        self.assertNotIn("按排序前列", answer.text)

    def test_data_detail_answer_reports_displayed_rows(self):
        answer = assemble_answer(
            [
                {
                    "node_id": "detail",
                    "type": "query_data",
                    "view": "detail",
                    "status": "accepted",
                    "answer": "不应出现在聊天区的大段明细",
                    "rows_fetched": 500,
                    "total_count": 20108,
                }
            ],
            response_mode="data",
        )

        self.assertIn("20108", answer.text)
        self.assertIn("前 500 条", answer.text)
        self.assertNotIn("不应出现在聊天区", answer.text)

    def test_analysis_answer_keeps_the_conclusion_only(self):
        conclusion = "长沙－芷江高速公路综合指标最好，判断维度是数量、面积和密度。"
        answer = assemble_answer(
            [
                {
                    "node_id": "q1",
                    "type": "query_data",
                    "view": "distribution",
                    "status": "accepted",
                    "answer": "路线名称 A，数量 100",
                    "rows_fetched": 16,
                },
                {
                    "node_id": "q2_calc",
                    "type": "post_calc",
                    "status": "accepted",
                    "answer": conclusion,
                    "rows_fetched": 1,
                },
            ],
            response_mode="analysis",
        )

        self.assertEqual(answer.text, conclusion)
        self.assertEqual(len(answer.sections), 2)
        self.assertEqual(answer.sections[0]["node_id"], "q1")
        self.assertEqual(answer.sections[1]["node_id"], "q2_calc")

    def test_analysis_answer_keeps_upstream_partial_coverage_evidence(self):
        conclusion = "长沙－芷江高速公路的病害数量高于另外两条路线。"
        coverage = (
            "长沙－芷江高速公路现有病害记录的采集时间在 "
            "2026-10-27 09:56 至 2026-11-04 10:37"
        )
        answer = assemble_answer(
            [
                {
                    "node_id": "q1",
                    "type": "query_data",
                    "view": "distribution",
                    "status": "accepted",
                    "coverage_verdict": PARTIAL,
                    "coverage_detail": coverage,
                    "rows_fetched": 6,
                },
                {
                    "node_id": "q2",
                    "type": "post_calc",
                    "status": "accepted",
                    "answer": conclusion,
                    "rows_fetched": 0,
                },
            ],
            response_mode="analysis",
        )

        self.assertIn(conclusion, answer.text)
        self.assertIn("2026-10-27", answer.text)
        self.assertIn("覆盖边界", answer.text)

    def test_partial_coverage_downgrades_conclusion_scope(self):
        answer = assemble_answer(
            [
                {
                    "node_id": "q1",
                    "type": "query_data",
                    "view": "top_groups",
                    "status": "accepted",
                    "coverage_verdict": PARTIAL,
                    "coverage_detail": (
                        "全库病害台账现有病害记录的采集时间在 "
                        "2026-06-18 19:58 至 2026-11-04 16:20"
                    ),
                    "answer": (
                        "全路网中检出病害数量最多的前5条路线是"
                        "长沙－芷江高速公路、平江－洞口高速公路等"
                    ),
                }
            ],
            response_mode="data",
        )

        self.assertTrue(
            answer.text.startswith(
                "覆盖范围不完整：以下结果只代表实际有记录的部分；"
                "未覆盖区间的数据可能改变总量、趋势或排名，"
            )
        )
        self.assertIn("这里只能给出已覆盖部分的观察结果", answer.text)
        self.assertIn("2026-11-04", answer.text)

    def test_large_table_is_compressed_to_metric_boundaries(self):
        rows = [
            {"路线名称": "A", "数量": 100, "面积": 80, "路线长度": 20},
            {"路线名称": "B", "数量": 40, "面积": 30, "路线长度": 10},
            {"路线名称": "C", "数量": 5, "面积": 2, "路线长度": 50},
        ]

        evidence = compact_table_evidence(rows)

        self.assertEqual(evidence["row_count"], 3)
        self.assertEqual(evidence["metrics"]["数量"]["max"]["group"], "A")
        self.assertEqual(evidence["metrics"]["数量"]["min"]["group"], "C")
        self.assertEqual(evidence["metrics"]["面积"]["max"]["group"], "A")
        self.assertEqual(evidence["metrics"]["路线长度"]["min"]["group"], "B")

    def test_compact_evidence_keeps_the_leader_within_each_outer_group(self):
        rows = [
            {"上下行方向": "上行", "病害类型": "条状修补", "数量": 14562},
            {"上下行方向": "上行", "病害类型": "纵向裂缝", "数量": 1023},
            {"上下行方向": "下行", "病害类型": "横向裂缝", "数量": 833},
            {"上下行方向": "下行", "病害类型": "条状修补", "数量": 10708},
        ]

        evidence = compact_table_evidence(rows)
        summary = evidence["group_summaries"][0]
        down = next(
            item
            for item in summary["groups"]
            if item["group"]["上下行方向"] == "下行"
        )

        self.assertEqual(summary["parent_columns"], ["上下行方向"])
        self.assertEqual(summary["detail_columns"], ["病害类型"])
        self.assertEqual(down["metrics"]["数量"]["max"]["value"], 10708.0)
        self.assertEqual(
            down["metrics"]["数量"]["max"]["row"],
            {"病害类型": "条状修补"},
        )

    @patch("app.calculations.domain.analysis.chat")
    def test_fixed_filter_context_is_not_a_group_main_item(self, chat):
        chat.return_value = "上行和下行都由条状修补排在首位。"

        payload = execute_post_calc(
            {
                "id": "p1",
                "question": "比较上行、下行各自累计长度最高的病害类型",
                "depends_on": ["up", "down"],
                "required_assertions": [
                    "multi_metric_comparison",
                    "analysis_only",
                ],
            },
            {
                "up": {
                    "question": "统计上行累计长度最高的病害类型",
                    "rows": [
                        {
                            "病害类型": "条状修补",
                            "上下行方向": "上行",
                            "有效病害总长度": 115.36,
                        }
                    ],
                    "row_count": 1,
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "up_or_down": "上下行方向",
                        "sum_effective_disease_length": "有效病害总长度",
                    },
                    "group_labels": {"上下行方向": "上行"},
                },
                "down": {
                    "question": "统计下行累计长度最高的病害类型",
                    "rows": [
                        {
                            "病害类型": "条状修补",
                            "上下行方向": "下行",
                            "有效病害总长度": 1340.8,
                        }
                    ],
                    "row_count": 1,
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "up_or_down": "上下行方向",
                        "sum_effective_disease_length": "有效病害总长度",
                    },
                    "group_labels": {"上下行方向": "下行"},
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("各分组的主项相同", payload["reply"])
        self.assertNotIn("各分组的主项不完全相同", payload["reply"])

    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=RuntimeError("model unavailable"),
    )
    def test_analysis_falls_back_to_deterministic_metric_boundaries(self, _chat):
        payload = execute_post_calc(
            {
                "question": "哪条路的健康状况最好？",
                "depends_on": [1],
                "required_assertions": [
                    "lower_is_better",
                    "analysis_only",
                ],
            },
            {
                1: {
                    "rows": [
                        {"路线名称": "A", "数量": 100, "面积": 80},
                        {"路线名称": "B", "数量": 40, "面积": 30},
                        {"路线名称": "C", "数量": 5, "面积": 2},
                    ],
                    "columns": ["路线名称", "数量", "面积"],
                }
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("C", payload["reply"])
        self.assertIn("代理指标", payload["reply"])

    @patch(
        "app.calculations.domain.analysis.chat",
        return_value="判断维度与依据：",
    )
    def test_incomplete_analysis_reply_falls_back_to_metric_boundaries(self, _chat):
        payload = execute_post_calc(
            {
                "question": "哪条路的健康状况最好？",
                "depends_on": [1],
                "required_assertions": [
                    "lower_is_better",
                    "analysis_only",
                ],
            },
            {
                1: {
                    "rows": [
                        {"路线名称": "A", "数量": 100, "面积": 80},
                        {"路线名称": "B", "数量": 40, "面积": 30},
                        {"路线名称": "C", "数量": 5, "面积": 2},
                    ],
                    "columns": ["路线名称", "数量", "面积"],
                }
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("C", payload["reply"])
        self.assertIn("代理指标", payload["reply"])

    def test_sql_expression_month_dimension_is_canonicalized(self):
        dimensions = _canonical_dimensions(
            "patrol",
            ("DATE_FORMAT(create_time, '%Y-%m')",),
        )

        self.assertEqual(dimensions, ("month",))


if __name__ == "__main__":
    unittest.main()
