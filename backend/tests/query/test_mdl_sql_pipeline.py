import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.compiler.pipeline import (
    run_mdl_sql_pipeline,
)
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.query.mdl_sql.errors import (
    MdlSqlContextError,
    MdlSqlPipelineError,
    MdlSqlRelationshipError,
)
from app.query.mdl_sql.models import (
    CompilerFailure,
    DryPlanResult,
    MdlSqlCandidate,
    SqlAuditReport,
    SqlIssue,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


def count_spec() -> QuerySpec:
    return QuerySpec(
        id="q1",
        kind="mysql",
        subject="disease",
        view="count",
        metrics=("count",),
        dimensions=(),
        filters=(),
        output_projection=("count",),
        order_by=(),
        limit=None,
        required_assertions=("preserve_user_scope",),
        question="病害有多少处？",
    )


def accepted_audit(
    *,
    used_models: tuple[str, ...] = ("disease",),
    used_relationships: tuple[str, ...] = (),
) -> SqlAuditReport:
    return SqlAuditReport(
        accepted=True,
        issues=(),
        root_model="disease",
        used_models=used_models,
        used_relationships=used_relationships,
        normalized_structure={"root_query_type": "select"},
    )


def failed_audit(code: str = "missing_filter") -> SqlAuditReport:
    return SqlAuditReport(
        accepted=False,
        issues=(
            SqlIssue(
                code=code,
                message="test audit failure",
                path="$",
                evidence={"reason": "test"},
            ),
        ),
    )


def accepted_dry_plan(candidate: MdlSqlCandidate) -> DryPlanResult:
    return DryPlanResult(
        accepted=True,
        mdl_sql=candidate.mdl_sql,
        physical_sql="SELECT COUNT(*) AS count FROM ai_problem",
        contract_hash=candidate.contract_hash,
    )


def failed_dry_plan(
    candidate: MdlSqlCandidate,
    code: str = "dry_plan_failed",
) -> DryPlanResult:
    return DryPlanResult(
        accepted=False,
        mdl_sql=candidate.mdl_sql,
        error=code,
        evidence={"failure_code": code, "reason": code},
        contract_hash=candidate.contract_hash,
    )


class MdlSqlPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        cls.snapshot = load_mdl_snapshot(
            "mysql",
            project_dir=PROJECT_DIR,
        )

    def contract(self):
        return build_mdl_query_contract(
            count_spec(),
            self.registry,
            self.snapshot,
            plan_id="plan-test:q1",
        )

    def candidate(
        self,
        source: str,
        *,
        mdl_sql: str = "SELECT COUNT(*) AS count FROM disease",
        used_models: tuple[str, ...] = ("disease",),
        used_relationship_ids: tuple[str, ...] = (),
        failure: CompilerFailure | None = None,
    ) -> MdlSqlCandidate:
        contract = self.contract()
        return MdlSqlCandidate(
            source=source,
            mdl_sql=mdl_sql,
            contract_id=contract.contract_id,
            contract_hash=contract.contract_hash,
            used_models=used_models,
            used_relationship_ids=used_relationship_ids,
            failure=failure,
        )

    def run_pipeline(self, fallback):
        return run_mdl_sql_pipeline(
            count_spec(),
            plan_id="plan-test:q1",
            registry=self.registry,
            mdl_snapshot=self.snapshot,
            generate_mdl_sql=fallback,
        )

    def test_deterministic_success_skips_fallback_and_returns_physical_sql(self):
        fallback = Mock()
        deterministic = self.candidate("deterministic")
        with (
            patch(
                "app.query.mdl_sql.compiler.pipeline.compile_contract_to_mdl_sql",
                return_value=deterministic,
            ),
            patch(
                "app.query.mdl_sql.compiler.pipeline.audit_mdl_sql",
                return_value=accepted_audit(),
            ),
            patch(
                "app.query.mdl_sql.compiler.pipeline.dry_plan_candidate",
                return_value=accepted_dry_plan(deterministic),
            ),
        ):
            result = self.run_pipeline(fallback)

        fallback.assert_not_called()
        self.assertEqual(result.candidate.source, "deterministic")
        self.assertEqual(result.contract.root_model, "disease")
        self.assertEqual(
            result.physical_sql,
            "SELECT COUNT(*) AS count FROM ai_problem",
        )
        self.assertIsNone(result.deterministic_failure)

    def test_deterministic_compile_failure_uses_one_fallback(self):
        fallback_candidate = self.candidate("llm_fallback")
        failure = CompilerFailure(
            code="compiler_not_covered",
            stage="deterministic",
            evidence={"reason": "unsupported expression"},
            contract_id=fallback_candidate.contract_id,
            contract_hash=fallback_candidate.contract_hash,
        )
        failed = replace(
            fallback_candidate,
            source="deterministic",
            mdl_sql="",
            failure=failure,
        )
        fallback = Mock(return_value=fallback_candidate)
        with (
            patch(
                "app.query.mdl_sql.compiler.pipeline.compile_contract_to_mdl_sql",
                return_value=failed,
            ),
            patch(
                "app.query.mdl_sql.compiler.pipeline.audit_mdl_sql",
                return_value=accepted_audit(),
            ),
            patch(
                "app.query.mdl_sql.compiler.pipeline.dry_plan_candidate",
                return_value=accepted_dry_plan(fallback_candidate),
            ),
        ):
            result = self.run_pipeline(fallback)

        fallback.assert_called_once()
        self.assertEqual(result.candidate.source, "llm_fallback")
        self.assertEqual(
            result.deterministic_failure.code,
            "compiler_not_covered",
        )

    def test_each_deterministic_post_compile_failure_uses_one_fallback(self):
        fallback_candidate = self.candidate("llm_fallback")
        deterministic = self.candidate("deterministic")
        cases = {
            "audit": {
                "audit": [failed_audit(), accepted_audit()],
                "dry": [accepted_dry_plan(fallback_candidate)],
                "stage": "audit",
                "code": "missing_filter",
            },
            "dry_plan": {
                "audit": [
                    accepted_audit(),
                    accepted_audit(),
                ],
                "dry": [
                    failed_dry_plan(deterministic),
                    accepted_dry_plan(fallback_candidate),
                ],
                "stage": "dry_plan",
                "code": "dry_plan_failed",
            },
            "physical": {
                "audit": [
                    accepted_audit(),
                    accepted_audit(),
                ],
                "dry": [
                    failed_dry_plan(
                        deterministic,
                        code="physical_sql_invalid",
                    ),
                    accepted_dry_plan(fallback_candidate),
                ],
                "stage": "physical",
                "code": "physical_sql_invalid",
            },
        }

        for name, case in cases.items():
            with self.subTest(stage=name):
                fallback = Mock(return_value=fallback_candidate)
                with (
                    patch(
                        "app.query.mdl_sql.compiler.pipeline.compile_contract_to_mdl_sql",
                        return_value=deterministic,
                    ),
                    patch(
                        "app.query.mdl_sql.compiler.pipeline.audit_mdl_sql",
                        side_effect=case["audit"],
                    ),
                    patch(
                        "app.query.mdl_sql.compiler.pipeline.dry_plan_candidate",
                        side_effect=case["dry"],
                    ),
                ):
                    result = self.run_pipeline(fallback)

                fallback.assert_called_once()
                self.assertEqual(result.candidate.source, "llm_fallback")
                self.assertEqual(
                    result.deterministic_failure.stage,
                    case["stage"],
                )
                self.assertEqual(
                    result.deterministic_failure.code,
                    case["code"],
                )

    def test_contract_context_and_relationship_failures_never_call_fallback(self):
        fallback = Mock()

        with (
            patch(
                "app.query.mdl_sql.compiler.pipeline.build_prompt_context",
                side_effect=MdlSqlContextError(
                    "context_budget_exceeded",
                    {"reason": "too_large"},
                ),
            ),
            self.assertRaises(MdlSqlPipelineError) as context_error,
        ):
            self.run_pipeline(fallback)
        fallback.assert_not_called()
        self.assertEqual(
            context_error.exception.failure.code,
            "context_budget_exceeded",
        )
        self.assertEqual(
            context_error.exception.failure.stage,
            "context",
        )

        fallback.reset_mock()
        with (
            patch(
                "app.query.mdl_sql.compiler.pipeline.build_relationship_subgraph",
                side_effect=MdlSqlRelationshipError(
                    "relationship_path_ambiguous",
                    {"reason": "multiple_paths"},
                ),
            ),
            self.assertRaises(MdlSqlPipelineError) as relationship_error,
        ):
            self.run_pipeline(fallback)
        fallback.assert_not_called()
        self.assertEqual(
            relationship_error.exception.failure.code,
            "relationship_path_ambiguous",
        )
        self.assertEqual(
            relationship_error.exception.failure.stage,
            "relationship",
        )

        fallback.reset_mock()
        with self.assertRaises(MdlSqlPipelineError) as contract_error:
            run_mdl_sql_pipeline(
                replace(count_spec(), subject="not_registered"),
                plan_id="plan-test:q1",
                registry=self.registry,
                mdl_snapshot=self.snapshot,
                generate_mdl_sql=fallback,
            )
        fallback.assert_not_called()
        self.assertEqual(
            contract_error.exception.failure.stage,
            "contract",
        )

    def test_relationship_error_during_contract_build_is_classified(self):
        fallback = Mock()

        with (
            patch(
                "app.query.mdl_sql.compiler.pipeline.build_mdl_query_contract",
                side_effect=MdlSqlRelationshipError(
                    "relationship_path_ambiguous",
                    {"reason": "multiple_paths"},
                ),
            ),
            self.assertRaises(MdlSqlPipelineError) as caught,
        ):
            self.run_pipeline(fallback)

        fallback.assert_not_called()
        self.assertEqual(
            caught.exception.failure.code,
            "relationship_path_ambiguous",
        )
        self.assertEqual(
            caught.exception.failure.stage,
            "relationship",
        )

    def test_fallback_audit_dry_plan_and_physical_failures_stop_immediately(self):
        fallback_candidate = self.candidate("llm_fallback")
        deterministic_failure = CompilerFailure(
            code="compiler_not_covered",
            stage="deterministic",
            contract_id=fallback_candidate.contract_id,
            contract_hash=fallback_candidate.contract_hash,
        )
        deterministic = replace(
            fallback_candidate,
            source="deterministic",
            mdl_sql="",
            failure=deterministic_failure,
        )
        cases = {
            "audit": {
                "audit": failed_audit(),
                "dry": accepted_dry_plan(fallback_candidate),
                "stage": "audit",
                "code": "missing_filter",
            },
            "dry_plan": {
                "audit": accepted_audit(),
                "dry": failed_dry_plan(fallback_candidate),
                "stage": "dry_plan",
                "code": "dry_plan_failed",
            },
            "physical": {
                "audit": accepted_audit(),
                "dry": failed_dry_plan(
                    fallback_candidate,
                    code="physical_sql_invalid",
                ),
                "stage": "physical",
                "code": "physical_sql_invalid",
            },
        }

        for name, case in cases.items():
            with self.subTest(stage=name):
                fallback = Mock(return_value=fallback_candidate)
                with (
                    patch(
                        "app.query.mdl_sql.compiler.pipeline.compile_contract_to_mdl_sql",
                        return_value=deterministic,
                    ),
                    patch(
                        "app.query.mdl_sql.compiler.pipeline.audit_mdl_sql",
                        return_value=case["audit"],
                    ),
                    patch(
                        "app.query.mdl_sql.compiler.pipeline.dry_plan_candidate",
                        return_value=case["dry"],
                    ),
                    self.assertRaises(MdlSqlPipelineError) as caught,
                ):
                    self.run_pipeline(fallback)

                fallback.assert_called_once()
                failure = caught.exception.failure
                self.assertEqual(failure.stage, case["stage"])
                self.assertEqual(failure.code, case["code"])
                self.assertEqual(
                    failure.contract_hash,
                    fallback_candidate.contract_hash,
                )
                self.assertEqual(
                    failure.mdl_sql,
                    fallback_candidate.mdl_sql,
                )
                self.assertTrue(failure.evidence)

    def test_fallback_declared_scope_must_match_sql_ast(self):
        fallback_candidate = self.candidate(
            "llm_fallback",
            used_models=("route",),
        )
        deterministic_failure = CompilerFailure(
            code="compiler_not_covered",
            stage="deterministic",
            contract_id=fallback_candidate.contract_id,
            contract_hash=fallback_candidate.contract_hash,
        )
        deterministic = replace(
            fallback_candidate,
            source="deterministic",
            mdl_sql="",
            used_models=("disease",),
            failure=deterministic_failure,
        )

        with (
            patch(
                "app.query.mdl_sql.compiler.pipeline.compile_contract_to_mdl_sql",
                return_value=deterministic,
            ),
            patch(
                "app.query.mdl_sql.compiler.pipeline.audit_mdl_sql",
                return_value=accepted_audit(used_models=("disease",)),
            ),
            self.assertRaises(MdlSqlPipelineError) as caught,
        ):
            self.run_pipeline(Mock(return_value=fallback_candidate))

        self.assertEqual(
            caught.exception.failure.code,
            "llm_declared_scope_mismatch",
        )
        self.assertEqual(
            caught.exception.failure.evidence["declared_models"],
            ("route",),
        )
        self.assertEqual(
            caught.exception.failure.evidence["audited_models"],
            ("disease",),
        )


if __name__ == "__main__":
    unittest.main()
