from pathlib import Path
import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec
from app.query.mdl_sql import CompilerFailure, MdlSqlPipelineError
from app.query.service import _default_wren_executor
from app.services import nl2sql, sql_engine


LEGACY_PROMPTS = (
    "query_semantic.md",
    "table_selector.md",
    "sql_mdl.md",
    "sql_physical.md",
    "optimize_question.md",
)


def _count_spec() -> QuerySpec:
    return QuerySpec(
        id="q1",
        kind="mysql",
        subject="disease",
        view="count",
        metrics=("count",),
        dimensions=(),
        filters=(),
        output_projection=(),
        order_by=(),
        limit=None,
        required_assertions=("preserve_user_scope",),
        question="泛油有多少处？",
    )


class LegacyQueryChainRemovedTests(unittest.TestCase):
    def test_legacy_query_prompts_are_deleted(self):
        prompt_dir = Path(__file__).resolve().parents[3] / "prompts"
        existing = [name for name in LEGACY_PROMPTS if (prompt_dir / name).exists()]

        self.assertEqual(existing, [])

    @patch("app.services.nl2sql.build_query_plan")
    @patch(
        "app.query.service.compile_queryspec_to_physical",
        side_effect=MdlSqlPipelineError(
            CompilerFailure(
                code="compiler_not_covered",
                stage="deterministic",
            )
        ),
    )
    def test_unsupported_queryspec_never_falls_back_to_legacy_planner(
        self,
        _compile_pipeline,
        build_plan,
    ):
        with self.assertRaises(MdlSqlPipelineError):
            _default_wren_executor(_count_spec())

        build_plan.assert_not_called()

    def test_legacy_semantic_and_table_selection_are_disabled(self):
        for call in (
            lambda: nl2sql.parse_semantics("泛油有多少处？"),
            lambda: nl2sql.build_query_plan("泛油有多少处？"),
            lambda: nl2sql.select_tables("泛油有多少处？"),
            lambda: nl2sql.refine_question("泛油有多少处？", {}, "", {}),
            lambda: nl2sql.expand_question_for_planning("泛油有多少处？"),
        ):
            with self.assertRaisesRegex(RuntimeError, "旧查询链路已停用"):
                call()

    def test_legacy_sql_generators_are_disabled_before_llm_call(self):
        calls = (
            lambda: sql_engine.generate_mdl_sql(
                "泛油有多少处？",
                "models",
                [],
                {},
            ),
            lambda: sql_engine.generate_sql(
                "泛油有多少处？",
                "mysql",
                "schema",
                "",
                [],
                {},
            ),
        )
        for call in calls:
            with self.assertRaisesRegex(RuntimeError, "旧查询链路已停用"):
                call()
