import unittest

from app.query.compiler import CompiledQuery
from app.query.contracts import ExecutionObservation, QuerySpec
from app.query.verifier import verify_result
from app.query.contracts import ValueBinding
from tests.query.factories import count_spec, detail_spec


def compiled_detail() -> CompiledQuery:
    return CompiledQuery(
        "SELECT id FROM ai_problem LIMIT 500",
        "wren",
        "wren",
    )


def detail_observation(
    rows_fetched: int,
    total_count: int,
    truncated: bool,
) -> ExecutionObservation:
    return ExecutionObservation(
        node_id="q1",
        ok=True,
        engine="wren",
        sql="SELECT id FROM ai_problem LIMIT 500",
        columns=("id",),
        row_count=rows_fetched,
        preview_rows=({"id": "1"},),
        rows_fetched=rows_fetched,
        truncated=truncated,
        total_count=total_count,
    )


def count_sql() -> CompiledQuery:
    return CompiledQuery("SELECT COUNT(*) AS n FROM ai_problem", "wren", "wren")


def count_observation() -> ExecutionObservation:
    return ExecutionObservation(
        node_id="q1",
        ok=True,
        engine="wren",
        sql="SELECT COUNT(*) AS n FROM ai_problem",
        columns=("n",),
        row_count=1,
        preview_rows=({"n": "20108"},),
        rows_fetched=1,
    )


class VerifierTests(unittest.TestCase):
    def test_aggregate_result_keeps_every_requested_metric_column(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count", "total_area", "average_area"),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*) AS count FROM ai_problem",
            columns=("数量",),
            row_count=1,
            preview_rows=({"数量": "10"},),
            result_rows=({"数量": "10"},),
            rows_fetched=1,
        )

        report = verify_result(
            spec,
            CompiledQuery(observation.sql, "wren", "wren"),
            observation,
        )

        self.assertEqual(report.status, "repairable")
        self.assertIn(
            "missing_metric_columns",
            {item.code for item in report.issues},
        )

    def test_empty_detail_does_not_require_output_columns(self):
        spec = QuerySpec(
            id="detail",
            kind="mysql",
            subject="disease",
            view="detail",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=("problem_number",),
            order_by=(),
            limit=500,
            required_assertions=(),
        )
        observation = ExecutionObservation(
            node_id="detail",
            ok=True,
            engine="wren",
            sql="SELECT problem_number FROM ai_problem WHERE 1=0",
            columns=(),
            rows_fetched=0,
        )

        report = verify_result(
            spec,
            CompiledQuery(observation.sql, "wren", "wren"),
            observation,
        )

        self.assertNotIn(
            "missing_output_columns",
            {issue.code for issue in report.issues},
        )

    def test_bounded_empty_distribution_is_valid_zero_result(self):
        spec = detail_spec(
            id="q1",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(
                ValueBinding(
                    field="collection_time",
                    op="between",
                    storage_value=(
                        "2026-11-02 00:00:00",
                        "2026-11-03 00:00:00",
                    ),
                    display_value="2026-11-02",
                    source="user_explicit",
                    resolver="time_range",
                    raw_text="2026-11-02",
                ),
            ),
            output_projection=("problem_type_name", "count"),
            limit=None,
        )
        report = verify_result(
            spec,
            compiled_detail(),
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT 1",
                columns=("病害类型", "数量"),
                row_count=0,
                rows_fetched=0,
                preview_rows=(),
            ),
        )
        self.assertEqual(report.status, "accepted")

    def test_distribution_rejects_null_group_label(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("count",),
            dimensions=("device",),
            filters=(),
            output_projection=("device",),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT device_code, COUNT(*) FROM patrol GROUP BY device_code",
            columns=("设备", "次数"),
            row_count=1,
            preview_rows=({"设备": "NULL", "次数": "20"},),
            rows_fetched=1,
        )
        report = verify_result(
            spec,
            CompiledQuery(observation.sql, "wren", "wren"),
            observation,
            semantic_checker=lambda *_: (),
            presentation_checker=lambda *_: (),
        )
        self.assertEqual(report.status, "repairable")
        self.assertIn(
            "unusable_group_label",
            {item.code for item in report.issues},
        )

    def test_large_detail_uses_metadata_not_full_rows(self):
        report = verify_result(
            spec=detail_spec(),
            compiled=compiled_detail(),
            observation=detail_observation(
                rows_fetched=500,
                total_count=20108,
                truncated=True,
            ),
            semantic_checker=lambda *_: (_ for _ in ()).throw(
                AssertionError("must not scan all rows")
            ),
            presentation_checker=lambda *_: (),
        )
        self.assertEqual(report.status, "accepted")

    def test_presentation_failure_requests_presentation_only(self):
        report = verify_result(
            spec=count_spec(),
            compiled=count_sql(),
            observation=count_observation(),
            semantic_checker=lambda *_: (),
            presentation_checker=lambda *_: ("missing_unit",),
        )
        self.assertEqual(report.status, "repairable")
        self.assertEqual(report.allowed_actions, ("repair_presentation",))
