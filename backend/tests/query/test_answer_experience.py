import unittest
from unittest.mock import patch

from app.query.analysis import _canonical_dimensions, analyze_single_turn
from app.query.contracts import QuerySpec
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import assemble_answer
from app.services import agent


class AnswerExperienceTests(unittest.TestCase):
    @patch("app.services.agent.chat", side_effect=AssertionError("LLM should not be called"))
    def test_structured_trend_uses_sequence_and_metric_specs(self, _chat):
        payload = agent._post_calc_payload(
            {
                "id": "q3",
                "question": "计算最近巡查的病害数量变化",
                "depends_on": [1, 2],
                "view": "trend",
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
        self.assertEqual([row["数量"] for row in payload["rows"]], [8, 3, 5])

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
            "哪条路的健康状况最好？从哪几个维度判断？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.response_mode, "analysis")
        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
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

    def test_large_table_is_compressed_to_metric_boundaries(self):
        rows = [
            {"路线名称": "A", "数量": 100, "面积": 80, "路线长度": 20},
            {"路线名称": "B", "数量": 40, "面积": 30, "路线长度": 10},
            {"路线名称": "C", "数量": 5, "面积": 2, "路线长度": 50},
        ]

        evidence = agent._compact_table_evidence(rows)

        self.assertEqual(evidence["row_count"], 3)
        self.assertEqual(evidence["metrics"]["数量"]["max"]["group"], "A")
        self.assertEqual(evidence["metrics"]["数量"]["min"]["group"], "C")
        self.assertEqual(evidence["metrics"]["面积"]["max"]["group"], "A")
        self.assertEqual(evidence["metrics"]["路线长度"]["min"]["group"], "B")

    @patch("app.services.agent.chat", side_effect=RuntimeError("model unavailable"))
    def test_analysis_falls_back_to_deterministic_metric_boundaries(self, _chat):
        payload = agent._post_calc_payload(
            {
                "question": "哪条路的健康状况最好？",
                "depends_on": [1],
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

    @patch("app.services.agent.chat", return_value="判断维度与依据：")
    def test_incomplete_analysis_reply_falls_back_to_metric_boundaries(self, _chat):
        payload = agent._post_calc_payload(
            {
                "question": "哪条路的健康状况最好？",
                "depends_on": [1],
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
