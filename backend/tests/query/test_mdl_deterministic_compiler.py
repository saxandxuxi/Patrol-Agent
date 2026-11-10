import unittest
from dataclasses import replace
from pathlib import Path

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.audit import audit_mdl_sql, parse_mdl_sql
from app.query.mdl_sql.compiler import compile_contract_to_mdl_sql
from app.query.mdl_sql.context.prompt_context import build_prompt_context
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.query.mdl_sql.models import RelationshipSubgraph


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"
SUBJECT_MODELS = {
    "disease": "disease",
    "patrol": "patrol",
    "route": "route",
    "section": "section",
    "pile_point": "pile_point",
    "asset": "ai_asset_main",
    "work_order": "ai_work_order",
    "bridge": "basic_facilities_bridges",
    "tunnel": "basic_facilities_tunnels",
    "slope": "basic_facilities_slope",
    "project": "project_manage_project_main",
}


def binding(field: str, value: object) -> ValueBinding:
    return ValueBinding(
        field=field,
        op="=",
        storage_value=value,
        display_value=str(value),
        source="user_explicit",
        resolver="test",
        raw_text=str(value),
    )


def spec(
    *,
    spec_id: str,
    subject: str,
    view: str,
    metrics: tuple[str, ...] = (),
    dimensions: tuple[str, ...] = (),
    filters: tuple[ValueBinding, ...] = (),
    outputs: tuple[str, ...] = (),
    order_by: tuple[object, ...] = (),
    limit: int | None = None,
) -> QuerySpec:
    return QuerySpec(
        id=spec_id,
        kind="mysql",
        subject=subject,
        view=view,
        metrics=metrics,
        dimensions=dimensions,
        filters=filters,
        output_projection=outputs,
        order_by=order_by,
        limit=limit,
        required_assertions=("preserve_user_scope",),
        question=f"{subject} {view}",
    )


class MdlDeterministicCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        cls.snapshot = load_mdl_snapshot(
            "mysql",
            project_dir=PROJECT_DIR,
        )

    def compile(self, query_spec: QuerySpec):
        contract = build_mdl_query_contract(
            query_spec,
            self.registry,
            self.snapshot,
            plan_id="plan-compiler",
        )
        context = build_prompt_context(
            contract,
            self.registry,
            self.snapshot,
        )
        candidate = compile_contract_to_mdl_sql(contract, context)
        subgraph = RelationshipSubgraph(
            root_model=contract.root_model,
            models=contract.allowed_models,
            relationships=tuple(
                self.snapshot.relationships[relationship_id]
                for relationship_id in contract.required_relationship_ids
            ),
        )
        return contract, candidate, subgraph

    def test_every_registered_subject_count_compiles_to_audited_select(self):
        for subject, root_model in SUBJECT_MODELS.items():
            with self.subTest(subject=subject):
                contract, candidate, subgraph = self.compile(
                    spec(
                        spec_id=f"{subject}-count",
                        subject=subject,
                        view="count",
                        metrics=("count",),
                        outputs=("count",),
                    )
                )

                self.assertIsNone(candidate.failure)
                self.assertEqual(candidate.source, "deterministic")
                self.assertTrue(candidate.mdl_sql.strip())
                parsed = parse_mdl_sql(candidate.mdl_sql, "mysql")
                self.assertTrue(parsed.valid)
                self.assertEqual(parsed.root_query_type, "select")
                report = audit_mdl_sql(
                    candidate,
                    contract,
                    subgraph,
                    self.snapshot,
                    self.registry.snapshot.mdl_compilation,
                )
                self.assertTrue(report.accepted, report.issues)
                self.assertEqual(report.root_model, root_model)

    def test_six_view_shapes_use_generic_contract_compilation(self):
        cases = {
            "count": spec(
                spec_id="count",
                subject="disease",
                view="count",
                metrics=("count",),
                filters=(binding("route_level", "0"),),
                outputs=("count",),
            ),
            "list": spec(
                spec_id="list",
                subject="section",
                view="list",
                filters=(binding("route_level", "0"),),
                outputs=("id", "route_name"),
                limit=20,
            ),
            "distribution": spec(
                spec_id="distribution",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("category_name",),
                outputs=("category_name", "count"),
            ),
            "top_records": spec(
                spec_id="top-records",
                subject="section",
                view="top_records",
                outputs=("id", "route_name"),
                order_by=({"field": "id", "direction": "asc"},),
                limit=5,
            ),
            "top_groups": spec(
                spec_id="top-groups",
                subject="disease",
                view="top_groups",
                metrics=("count",),
                dimensions=("category_name",),
                outputs=("category_name", "count"),
                order_by=({"field": "count", "direction": "desc"},),
                limit=3,
            ),
            "trend": spec(
                spec_id="trend",
                subject="disease",
                view="trend",
                metrics=("count",),
                dimensions=("collection_time",),
                outputs=("collection_time", "count"),
            ),
        }

        for view, query_spec in cases.items():
            with self.subTest(view=view):
                contract, candidate, subgraph = self.compile(query_spec)

                self.assertIsNone(candidate.failure)
                self.assertTrue(candidate.mdl_sql.strip())
                report = audit_mdl_sql(
                    candidate,
                    contract,
                    subgraph,
                    self.snapshot,
                    self.registry.snapshot.mdl_compilation,
                )
                self.assertTrue(report.accepted, report.issues)

    def test_route_level_keeps_section_and_disease_roots_distinct(self):
        section = spec(
            spec_id="section-root",
            subject="section",
            view="list",
            filters=(binding("route_level", "0"),),
            outputs=("id", "route_name"),
            limit=20,
        )
        disease = spec(
            spec_id="disease-root",
            subject="disease",
            view="count",
            metrics=("count",),
            filters=(binding("route_level", "0"),),
            outputs=("count",),
        )

        section_contract, section_candidate, _ = self.compile(section)
        disease_contract, disease_candidate, _ = self.compile(disease)
        section_parsed = parse_mdl_sql(section_candidate.mdl_sql, "mysql")
        disease_parsed = parse_mdl_sql(disease_candidate.mdl_sql, "mysql")

        self.assertEqual(section_contract.root_model, "section")
        self.assertEqual(disease_contract.root_model, "disease")
        self.assertEqual(
            section_parsed.table_aliases["section"],
            "section",
        )
        self.assertEqual(
            disease_parsed.table_aliases["disease"],
            "disease",
        )
        self.assertIn("section_route", section_candidate.used_relationship_ids)
        self.assertIn("disease_route", disease_candidate.used_relationship_ids)
        self.assertIn("route.level", disease_parsed.qualified_columns)

    def test_uncovered_contract_returns_structured_failure(self):
        contract = build_mdl_query_contract(
            spec(
                spec_id="unsupported",
                subject="disease",
                view="count",
                metrics=("count",),
                outputs=("count",),
            ),
            self.registry,
            self.snapshot,
        )
        context = build_prompt_context(
            contract,
            self.registry,
            self.snapshot,
        )
        uncovered = replace(contract, outputs=())

        candidate = compile_contract_to_mdl_sql(uncovered, context)

        self.assertEqual(candidate.source, "deterministic")
        self.assertIsNotNone(candidate.failure)
        self.assertEqual(candidate.failure.stage, "deterministic")
        self.assertEqual(
            candidate.failure.code,
            "compiler_not_covered",
        )
        self.assertEqual(candidate.failure.contract_id, contract.contract_id)

    def test_expression_failure_is_not_returned_as_success_sql(self):
        contract = build_mdl_query_contract(
            spec(
                spec_id="broken-expression",
                subject="disease",
                view="count",
                metrics=("count",),
                outputs=("count",),
            ),
            self.registry,
            self.snapshot,
        )
        context = build_prompt_context(
            contract,
            self.registry,
            self.snapshot,
        )
        broken = replace(
            contract,
            outputs=(
                replace(contract.outputs[0], expression="SUM("),
            ),
        )

        candidate = compile_contract_to_mdl_sql(broken, context)

        self.assertIsNotNone(candidate.failure)
        self.assertEqual(
            candidate.failure.code,
            "compiler_exception",
        )
        self.assertEqual(candidate.mdl_sql, "")

    def test_compiler_sources_do_not_branch_on_subject_names(self):
        compiler_dir = REPO_ROOT / "backend" / "app" / "query" / "mdl_sql" / "compiler"
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in compiler_dir.glob("*.py")
        )

        self.assertNotIn('subject == "disease"', sources)
        self.assertNotIn('subject == "patrol"', sources)
        self.assertNotIn("subject ==", sources)


if __name__ == "__main__":
    unittest.main()
