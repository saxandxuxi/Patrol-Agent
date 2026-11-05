import unittest

from app.query.compiler import CompiledQuery
from app.query.contracts import ExecutionObservation
from app.query.verifier import verify_result
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
