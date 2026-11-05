import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.service import (
    _default_execute_sql,
    _default_wren_executor,
    _simple_spec_mdl,
)
from app.services import nl2sql


class CompilerAuthorityTests(unittest.TestCase):
    @patch("app.query.service.wren_engine.compile_mdl_sql_verbose", return_value=("SELECT 1", ""))
    @patch("app.query.service.sql_engine.generate_mdl_sql", return_value="MDL")
    @patch("app.query.service.nl2sql.retrieve_query_context", return_value={})
    @patch("app.query.service.wren_engine.mdl_context", return_value="models")
    @patch("app.query.service.nl2sql.select_tables", return_value=set())
    @patch("app.query.service.nl2sql.build_query_plan")
    def test_queryspec_filters_override_plan_hallucinations(
        self,
        build_plan,
        _select_tables,
        _mdl_context,
        _retrieve,
        generate_sql,
        _compile_sql,
    ):
        build_plan.return_value = {
            "kind": "mysql",
            "message": "统计第三季度不同病害在不同道路上的新增情况",
            "subject": "disease",
            "shape": "distribution",
            "required_conditions": [],
            "slots": {
                "semantic_conditions": [
                    {"field": "route_name", "op": "is_null", "value": None}
                ],
                "semantic_metrics": ["count"],
            },
        }
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

        _default_wren_executor(spec)

        conditions = generate_sql.call_args.args[2]
        self.assertFalse(any("route_name" in item and "NULL" in item for item in conditions))
        self.assertTrue(any("collection_time" in item and "2026-07-01" in item for item in conditions))

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

    def test_patrol_route_list_does_not_add_disease_count(self):
        sql = nl2sql.simple_patrol_route_list_mdl(
            {
                "kind": "mysql",
                "route_scope_from_patrol": True,
                "shape": "list",
                "required_conditions": [],
                "slots": {},
            }
        )
        self.assertIsNotNone(sql)
        self.assertNotIn("disease", sql.lower())
        self.assertNotIn("病害数量", sql)
        self.assertIn("r.short_name IS NOT NULL", sql)
        self.assertIn("r.short_name NOT IN ('未知路线')", sql)

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
