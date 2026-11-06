import unittest
from unittest.mock import patch

from app.services import nl2sql


def _scope():
    return {
        "text": "真实字段上下文",
        "source": "field_grounding",
        "table": "disease",
        "subject": "disease",
        "touched_columns": [],
        "physical_columns": [
            {
                "column": "route_name",
                "label": "路线名称",
                "type": "varchar",
                "code_map": {},
                "internal": False,
                "source": "schema_catalog",
            },
            {
                "column": "problem_type_name",
                "label": "病害类型",
                "type": "varchar",
                "code_map": {},
                "internal": False,
                "source": "schema_catalog",
            },
            {
                "column": "area",
                "label": "面积",
                "type": "decimal",
                "code_map": {},
                "internal": False,
                "source": "schema_catalog",
            },
            {
                "column": "collection_time",
                "label": "采集时间",
                "type": "datetime",
                "code_map": {},
                "internal": False,
                "source": "schema_catalog",
            },
        ],
        "display_columns": [
            {"column": "route_name", "label": "路线名称"},
            {"column": "problem_type_name", "label": "病害类型"},
            {"column": "area", "label": "面积"},
            {"column": "collection_time", "label": "采集时间"},
        ],
    }


class QuestionOptimizerTests(unittest.TestCase):
    @patch("app.services.nl2sql.question_optimization_context", return_value=_scope())
    @patch("app.services.nl2sql.chat")
    def test_invented_metrics_in_free_text_are_not_sent_to_planner(
        self,
        chat,
        _context,
    ):
        chat.return_value = """
        {
          "question": "把 PCI 指标最高的路线调出来",
          "required_dimensions": ["路线", "病害数量", "病害密度"],
          "required_fields": ["route_name", "area"],
          "required_metrics": ["count", "total_area", "density"],
          "include_detail": false
        }
        """

        result = nl2sql.expand_question_for_planning(
            "结合数据，你觉得养护资金应该投哪条路？"
        )

        self.assertIsNotNone(result)
        question = result["question"]
        self.assertNotIn("PCI", question)
        self.assertIn("route_name", question)
        self.assertIn("area", question)
        self.assertIn("养护资金", question)

    @patch("app.services.nl2sql.question_optimization_context", return_value=_scope())
    @patch("app.services.nl2sql.chat")
    def test_nonexistent_required_column_rejects_optimization(
        self,
        chat,
        _context,
    ):
        chat.return_value = """
        {
          "question": "按 PCI 排名",
          "required_dimensions": ["路线"],
          "required_fields": ["pci"],
          "required_metrics": ["count"],
          "include_detail": false
        }
        """

        result = nl2sql.expand_question_for_planning(
            "结合数据，你觉得养护资金应该投哪条路？"
        )

        self.assertIsNone(result)

    @patch("app.services.nl2sql.question_optimization_context", return_value=_scope())
    @patch("app.services.nl2sql.chat")
    def test_scoped_detection_count_adds_a_detail_view(
        self,
        chat,
        _context,
    ):
        chat.return_value = """
        {
          "question": "统计 11 月 2 日裂缝数量",
          "required_dimensions": ["病害数量"],
          "required_fields": ["collection_time", "problem_type_name"],
          "required_metrics": ["count"],
          "include_detail": true
        }
        """

        result = nl2sql.expand_question_for_planning(
            "11月2日检出多少裂缝病害"
        )

        self.assertIsNotNone(result)
        question = result["question"]
        self.assertIn("collection_time", question)
        self.assertIn("problem_type_name", question)
        self.assertIn("先给出精确总数", question)
        self.assertIn("列出命中记录", question)


if __name__ == "__main__":
    unittest.main()
