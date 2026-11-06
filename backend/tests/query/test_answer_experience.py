import unittest
from unittest.mock import patch

from app.query.analysis import _canonical_dimensions, analyze_single_turn
from app.query.contracts import QuerySpec
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import assemble_answer
from app.services import agent


class AnswerExperienceTests(unittest.TestCase):
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
                    "metrics": ["count", "sum_area", "sum_length"],
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
                }
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
        self.assertTrue(
            {"total_area", "sum_area"}.intersection(plan.nodes[0].spec.metrics)
        )
        self.assertTrue(
            {"average_area", "avg_area"}.intersection(plan.nodes[0].spec.metrics)
        )
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

    def test_sql_expression_month_dimension_is_canonicalized(self):
        dimensions = _canonical_dimensions(
            "patrol",
            ("DATE_FORMAT(create_time, '%Y-%m')",),
        )

        self.assertEqual(dimensions, ("month",))


if __name__ == "__main__":
    unittest.main()
