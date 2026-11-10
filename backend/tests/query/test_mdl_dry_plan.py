import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.dry_plan import (
    check_physical_sql,
    dry_plan_candidate,
)
from app.query.mdl_sql.models import MdlSqlCandidate
from app.services import wren_engine


REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


class _Engine:
    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[str] = []

    def dry_plan(self, mdl_sql: str):
        self.calls.append(mdl_sql)
        if self.error is not None:
            raise self.error
        return self.value


class WrenDryPlanBoundaryTests(unittest.TestCase):
    def test_raw_boundary_normalizes_supported_engine_results(self):
        cases = (
            ("SELECT 1", "SELECT 1"),
            ({"sql": "SELECT 2"}, "SELECT 2"),
            ({"dialect_sql": "SELECT 3"}, "SELECT 3"),
            (None, None),
            ({}, None),
            ("   ", None),
        )
        for engine_result, expected in cases:
            with self.subTest(engine_result=engine_result):
                engine = _Engine(engine_result)
                with patch.object(
                    wren_engine,
                    "_engine",
                    return_value=engine,
                ):
                    physical, error = wren_engine.dry_plan_mdl_sql(
                        "mysql",
                        "SELECT disease.id FROM disease",
                    )

                self.assertEqual(physical, expected)
                self.assertEqual(error, "")
                self.assertEqual(
                    engine.calls,
                    ["SELECT disease.id FROM disease"],
                )

    def test_raw_boundary_captures_exception_as_error_text(self):
        engine = _Engine(error=RuntimeError("plan exploded"))
        with patch.object(
            wren_engine,
            "_engine",
            return_value=engine,
        ):
            physical, error = wren_engine.dry_plan_mdl_sql(
                "mysql",
                "SELECT disease.id FROM disease",
            )

        self.assertIsNone(physical)
        self.assertIn("plan exploded", error)

    def test_legacy_verbose_compiler_delegates_to_raw_boundary(self):
        with (
            patch.object(
                wren_engine.settings,
                "wren_enabled",
                True,
            ),
            patch.object(
                wren_engine,
                "model_names",
                return_value={"disease"},
            ),
            patch.object(
                wren_engine,
                "physical_table_names",
                return_value=set(),
            ),
            patch.object(
                wren_engine,
                "join_contract_issue",
                return_value="",
            ),
            patch.object(
                wren_engine,
                "dry_plan_mdl_sql",
                return_value=("SELECT 1", ""),
            ) as raw_boundary,
        ):
            physical, error = wren_engine.compile_mdl_sql_verbose(
                "mysql",
                "SELECT disease.id FROM disease",
            )

        self.assertEqual(physical, "SELECT 1")
        self.assertEqual(error, "")
        raw_boundary.assert_called_once_with(
            "mysql",
            "SELECT disease.id FROM disease",
        )


class StructuredDryPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_mdl_snapshot(
            "mysql",
            project_dir=PROJECT_DIR,
        )

    def candidate(self) -> MdlSqlCandidate:
        return MdlSqlCandidate(
            source="deterministic",
            mdl_sql=(
                "SELECT COUNT(*) AS count FROM disease "
                "WHERE disease.del_flag = 0"
            ),
            contract_id="plan:q1:abc",
            contract_hash="contract-hash",
            used_models=("disease",),
        )

    def test_success_returns_physical_sql_only_after_static_check(self):
        candidate = self.candidate()
        with (
            patch(
                "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
                return_value=(
                    "SELECT COUNT(*) AS count FROM ai_problem "
                    "WHERE del_flag = 0",
                    "",
                ),
            ),
            patch(
                "app.query.mdl_sql.dry_plan.static_check.check_sql",
                return_value=[],
            ) as check_sql,
        ):
            result = dry_plan_candidate(
                "mysql",
                candidate,
                self.snapshot,
            )

        self.assertTrue(result.accepted)
        self.assertEqual(result.mdl_sql, candidate.mdl_sql)
        self.assertIn("FROM ai_problem", result.physical_sql)
        self.assertEqual(result.contract_hash, candidate.contract_hash)
        self.assertEqual(result.error, "")
        check_sql.assert_called_once()

    def test_empty_engine_result_is_structured_failure(self):
        candidate = self.candidate()
        with (
            patch(
                "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
                return_value=(None, ""),
            ),
            patch(
                "app.query.mdl_sql.dry_plan.static_check.check_sql",
            ) as check_sql,
        ):
            result = dry_plan_candidate(
                "mysql",
                candidate,
                self.snapshot,
            )

        self.assertFalse(result.accepted)
        self.assertEqual(result.physical_sql, "")
        self.assertEqual(
            result.evidence["failure_code"],
            "dry_plan_failed",
        )
        self.assertEqual(result.contract_hash, candidate.contract_hash)
        self.assertIn("disease", result.evidence["allowed_models"])
        self.assertIn(
            "id",
            result.evidence["allowed_columns"]["disease"],
        )
        check_sql.assert_not_called()

    def test_raw_error_is_kept_with_structured_allowed_facts(self):
        candidate = self.candidate()
        raw_error = "column disease.fake_field not found"
        with (
            patch(
                "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
                return_value=(None, raw_error),
            ),
            patch(
                "app.query.mdl_sql.dry_plan.wren_engine.compile_error_hint",
                return_value="disease 可用字段：id、area",
            ),
        ):
            result = dry_plan_candidate(
                "mysql",
                candidate,
                self.snapshot,
            )

        self.assertFalse(result.accepted)
        self.assertEqual(result.physical_sql, "")
        self.assertEqual(result.evidence["raw_error"], raw_error)
        self.assertEqual(
            result.evidence["diagnostic_hint"],
            "disease 可用字段：id、area",
        )
        self.assertEqual(
            result.evidence["failure_code"],
            "dry_plan_failed",
        )

    def test_failed_candidate_is_not_sent_to_wren(self):
        candidate = self.candidate()
        failed = MdlSqlCandidate(
            source="deterministic",
            mdl_sql="",
            contract_id=candidate.contract_id,
            contract_hash=candidate.contract_hash,
            failure=Mock(),
        )
        with patch(
            "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        ) as raw_boundary:
            result = dry_plan_candidate(
                "mysql",
                failed,
                self.snapshot,
            )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.evidence["reason"],
            "candidate_already_failed",
        )
        self.assertEqual(result.contract_hash, candidate.contract_hash)
        raw_boundary.assert_not_called()


class PhysicalSqlCheckTests(unittest.TestCase):
    def test_physical_sql_passes_only_with_empty_problem_list(self):
        with patch(
            "app.query.mdl_sql.dry_plan.static_check.check_sql",
            return_value=[],
        ):
            result = check_physical_sql("mysql", "SELECT 1")

        self.assertTrue(result.accepted)
        self.assertEqual(result.physical_sql, "SELECT 1")
        self.assertEqual(result.evidence, {})

    def test_static_problem_returns_structured_failure(self):
        with patch(
            "app.query.mdl_sql.dry_plan.static_check.check_sql",
            return_value=["列不存在：ai_problem.fake"],
        ):
            result = check_physical_sql(
                "mysql",
                "SELECT fake FROM ai_problem",
            )

        self.assertFalse(result.accepted)
        self.assertEqual(result.physical_sql, "")
        self.assertEqual(
            result.evidence["failure_code"],
            "physical_sql_invalid",
        )
        self.assertEqual(
            result.evidence["problems"],
            ("列不存在：ai_problem.fake",),
        )

    def test_empty_physical_sql_fails_before_static_check(self):
        with patch(
            "app.query.mdl_sql.dry_plan.static_check.check_sql",
        ) as check_sql:
            result = check_physical_sql("mysql", "  ")

        self.assertFalse(result.accepted)
        self.assertEqual(result.physical_sql, "")
        self.assertEqual(
            result.evidence["failure_code"],
            "physical_sql_invalid",
        )
        check_sql.assert_not_called()


if __name__ == "__main__":
    unittest.main()
