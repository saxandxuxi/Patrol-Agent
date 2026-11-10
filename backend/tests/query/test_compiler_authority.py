import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.service import (
    _default_execute_sql,
    _default_wren_executor,
    _simple_spec_mdl,
)


class CompilerAuthorityTests(unittest.TestCase):
    @patch(
        "app.query.service.compile_queryspec_to_physical",
        return_value="SELECT 1",
    )
    def test_wren_compiles_the_frozen_queryspec_through_the_pipeline(
        self,
        compile_pipeline,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name", "route_name"),
            filters=(
                ValueBinding(
                    field="create_time",
                    op="between",
                    storage_value=(
                        "2026-07-01 00:00:00",
                        "2026-10-01 00:00:00",
                    ),
                    display_value="第三季度",
                    source="user_explicit",
                    resolver="time_range",
                    raw_text="第三季度",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计第三季度不同病害在不同道路上的新增情况",
        )

        self.assertEqual(
            _default_wren_executor(spec, plan_id="plan:q1"),
            "SELECT 1",
        )

        compile_pipeline.assert_called_once_with(
            spec,
            plan_id="plan:q1",
        )

    @patch("app.query.service.sql_engine.execute_sql", return_value={"columns": [], "rows": []})
    def test_execution_uses_source_aware_sql_boundary(self, execute_sql):
        _default_execute_sql(
            "mysql",
            "SELECT 1",
            question="统计第三季度新增病害",
        )
        execute_sql.assert_called_once_with(
            "mysql",
            "SELECT 1",
            question="统计第三季度新增病害",
        )

    def test_effective_length_sum_uses_calculated_field_in_where_clause(self):
        spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="sum",
            metrics=("sum_effective_disease_length",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="disease_length",
                    op=">",
                    storage_value="2",
                    display_value="2",
                    source="user_explicit",
                    resolver="numeric",
                    raw_text="大于2米",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="这些病害的总长度是多少米？",
        )
        sql = _simple_spec_mdl(spec)
        self.assertIsNotNone(sql)
        self.assertIn(
            "disease.effective_disease_length > 2",
            sql,
        )
        self.assertIn(
            "SUM(disease.effective_disease_length)",
            sql,
        )
