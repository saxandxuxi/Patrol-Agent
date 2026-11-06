import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.calculations.domain.comparison import compare_datasets
from app.calculations.post_calc import execute_post_calc
from app.services import llm, orchestrator, react_agent


class RuntimeBoundaryTests(unittest.TestCase):
    def test_empty_role_setting_uses_role_default_not_global_default(self):
        chosen = llm._role_choice(
            {
                "semantic_model": "",
                "planner_model": "",
                "_defaults": {
                    "semantic_model": "qwen3.7-plus",
                    "planner_model": "qwen3.7-plus",
                },
            },
            "semantic",
        )

        self.assertEqual(chosen, "qwen3.7-plus")

    @patch("app.services.llm._post")
    def test_reasoning_model_gets_enough_tokens_for_final_content(self, post):
        post.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "{}",
                        "reasoning_content": "reasoning",
                    }
                }
            ]
        }

        llm.chat(
            [{"role": "user", "content": "只输出 JSON"}],
            model={
                "api_key": "test",
                "base_url": "https://example.test",
                "model": "deepseek-reasoner",
                "provider": "deepseek",
            },
            max_tokens=1024,
        )

        payload = post.call_args.args[1]
        self.assertGreaterEqual(payload["max_tokens"], 4096)

    def test_success_trace_is_safe_on_gbk_console(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")
        obs = {
            "ok": True,
            "tool": "query_data",
            "summary": "⚠ 已完成",
            "data": {
                "question": "11月2日病害分布",
                "row_count": 1,
                "columns": ["数量"],
                "preview_rows": [{"数量": 1}],
            },
        }

        with redirect_stdout(stream):
            react_agent._print_success_observation(
                {"id": 1, "type": "data", "question": "11月2日病害分布"},
                obs,
            )
            sys.stdout.flush()

        self.assertIn(b"\\u26a0", buffer.getvalue())

    def test_result_section_title_identifies_sub_question(self):
        section = react_agent._section(
            {
                "id": 2,
                "type": "data",
                "question": "查询长沙－芷江高速公路11月3日的病害分布",
            },
            {
                "ok": True,
                "tool": "query_data",
                "summary": "已完成",
                "data": {
                    "question": "查询长沙－芷江高速公路11月3日的病害分布",
                    "row_count": 1,
                    "columns": ["数量"],
                    "preview_rows": [{"数量": 1}],
                },
            },
        )

        self.assertEqual(section["tool"], "query_data")
        self.assertIn("11月3日", section["title"])
        self.assertNotEqual(section["title"], "query_data")

    def test_comparison_promotes_analysis_to_post_calc(self):
        self.assertTrue(
            orchestrator._should_execute_as_post_calc(
                "对比11月2日与3日的病害差异"
            )
        )

    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=RuntimeError("model unavailable"),
    )
    def test_density_keeps_disease_type_and_route_context(self, _chat):
        result = execute_post_calc(
            {
                "question": "按病害类型和路线计算密度",
                "depends_on": [1],
                "required_assertions": ["lower_is_better"],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": "divide",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "1", "field": "count"},
                        },
                        {
                            "name": "denominator",
                            "source": {"node": "1", "field": "route_length_km"},
                        },
                    ],
                    "output": {
                        "name": "density",
                        "label": "病害密度",
                        "unit": "处/km",
                    },
                    "precision": 4,
                },
            },
            {
                1: {
                    "field_labels": {
                        "count": "数量",
                        "route_length_km": "长度",
                        "route_name": "路线",
                        "problem_type_name": "病害类型",
                    },
                    "rows": [
                        {
                            "路线": "A",
                            "病害类型": "坑槽",
                            "数量": "4",
                            "长度": "5.1",
                        },
                        {
                            "路线": "A",
                            "病害类型": "横向裂缝",
                            "数量": "56",
                            "长度": "5.1",
                        },
                    ],
                }
            },
        )

        self.assertTrue(result["accepted"])
        self.assertIn("病害类型", result["columns"])
        self.assertEqual(
            [row["病害类型"] for row in result["rows"]],
            ["坑槽", "横向裂缝"],
        )

    def test_comparison_accepts_two_empty_filtered_row_sets(self):
        result = compare_datasets(
            [
                {"columns": ["病害类型", "数量"], "rows": []},
                {"columns": ["病害类型", "数量"], "rows": []},
            ],
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["accepted"])
        self.assertIn("两组均无匹配记录", result["reply"])

    def test_react_analysis_receives_all_prior_row_sets(self):
        blackboard = react_agent.ReactBlackboard(
            full_question="对比两天的路段台账",
            intent="analysis",
            goal="对比两天的路段台账",
            plan={"steps": []},
        )
        for step_id, question, mileage in (
            (1, "查询11月2日路段台账", "1.00"),
            (2, "查询11月3日路段台账", "1.20"),
        ):
            blackboard.add(
                {"id": step_id, "type": "data", "question": question},
                {
                    "ok": True,
                    "tool": "query_data",
                    "summary": "1 行",
                    "data": {
                        "question": question,
                        "row_count": 1,
                        "columns": ["路段全称", "公路实际里程"],
                        "preview_rows": [
                            {"路段全称": "K1~K2", "公路实际里程": mileage}
                        ],
                    },
                },
            )

        obs, args, _ = react_agent._execute_step(
            blackboard,
            {
                "id": 3,
                "type": "analysis",
                "question": "对比11月2日与3日路段台账差异",
                "depends_on": [],
            },
        )

        self.assertEqual(args["task"]["depends_on"], [1, 2])
        self.assertEqual(set(args["results"]), {1, 2})
        self.assertTrue(obs["ok"])
        self.assertIn("字段变化 1", obs["summary"])
