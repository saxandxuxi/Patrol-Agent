import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.audit.ast import parse_mdl_sql
from app.query.mdl_sql.audit.safety_audit import audit_safety
from app.query.mdl_sql.contract_builder import build_mdl_query_contract


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


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


def section_contract(registry, snapshot):
    spec = QuerySpec(
        id="q1",
        kind="mysql",
        subject="section",
        view="list",
        metrics=(),
        dimensions=(),
        filters=(binding("route_level", "0"),),
        output_projection=("id", "route_name"),
        order_by=(),
        limit=10,
        required_assertions=("preserve_user_scope",),
        question="列出国道路段",
    )
    return build_mdl_query_contract(
        spec,
        registry,
        snapshot,
        plan_id="plan-audit",
    )


class MdlSqlAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        cls.snapshot = load_mdl_snapshot("mysql", project_dir=PROJECT_DIR)
        cls.contract = section_contract(cls.registry, cls.snapshot)

    def issue_codes(self, sql: str):
        parsed = parse_mdl_sql(sql, "mysql")
        issues = audit_safety(
            parsed,
            self.contract,
            self.snapshot,
            self.registry.snapshot.mdl_compilation,
        )
        return tuple(issue.code for issue in issues)

    def test_parser_records_select_facts_from_ast(self):
        parsed = parse_mdl_sql(
            "SELECT route.short_name AS route_name, COUNT(*) AS n "
            "FROM section JOIN route ON section.route_id = route.id "
            "WHERE section.del_flag = 0 "
            "GROUP BY route.short_name "
            "ORDER BY n DESC LIMIT 5",
            "mysql",
        )

        self.assertTrue(parsed.valid)
        self.assertEqual(parsed.root_query_type, "select")
        self.assertEqual(
            dict(parsed.table_aliases),
            {"section": "section", "route": "route"},
        )
        self.assertIn("section.route_id", parsed.qualified_columns)
        self.assertIn("route.id", parsed.qualified_columns)
        self.assertEqual(len(parsed.joins), 1)
        self.assertEqual(
            parsed.group_expressions,
            ("route.short_name",),
        )
        self.assertEqual(parsed.limit, 5)
        self.assertIsInstance(parsed.table_aliases, MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            parsed.limit = 1

    def test_parser_accepts_one_cte_select_and_rejects_multiple_statements(self):
        cte = parse_mdl_sql(
            "WITH x AS (SELECT id FROM section) SELECT COUNT(*) AS n FROM x",
            "mysql",
        )
        self.assertTrue(cte.valid)
        self.assertEqual(cte.root_query_type, "select")

        multiple = parse_mdl_sql(
            "SELECT COUNT(*) FROM section; SELECT COUNT(*) FROM route",
            "mysql",
        )
        self.assertFalse(multiple.valid)
        self.assertEqual(
            [issue.code for issue in multiple.issues],
            ["multiple_statements"],
        )

    def test_parser_fails_closed_without_repairing_markdown_or_empty_sql(self):
        for sql in ("```sql\nSELECT 1\n```", "", "   "):
            with self.subTest(sql=sql):
                parsed = parse_mdl_sql(sql, "mysql")
                self.assertFalse(parsed.valid)
                self.assertEqual(
                    [issue.code for issue in parsed.issues],
                    ["sql_parse_failed"],
                )

    def test_parser_rejects_every_forbidden_statement_type(self):
        statements = (
            "INSERT INTO section(id) VALUES (1)",
            "UPDATE section SET id = 1",
            "DELETE FROM section",
            "DROP TABLE section",
        )
        for sql in statements:
            with self.subTest(sql=sql):
                parsed = parse_mdl_sql(sql, "mysql")
                self.assertFalse(parsed.valid)
                self.assertIn(
                    "forbidden_sql_type",
                    [issue.code for issue in parsed.issues],
                )

    def test_safety_rejects_physical_only_tables(self):
        codes = self.issue_codes(
            "SELECT COUNT(*) AS n FROM basic_facilities_sections "
            "WHERE basic_facilities_sections.del_flag = 0"
        )

        self.assertEqual(codes, ("physical_table_reference",))

    def test_mdl_model_name_is_allowed_even_when_it_is_physical(self):
        parsed = parse_mdl_sql(
            "SELECT COUNT(*) AS n FROM ai_asset_main "
            "WHERE ai_asset_main.del_flag = 0",
            "mysql",
        )
        issues = audit_safety(
            parsed,
            self.contract,
            self.snapshot,
            self.registry.snapshot.mdl_compilation,
        )

        self.assertNotIn(
            "physical_table_reference",
            [issue.code for issue in issues],
        )

    def test_safety_rejects_dangerous_functions_and_bare_star(self):
        dangerous = self.issue_codes(
            "SELECT SLEEP(1) FROM section WHERE section.del_flag = 0"
        )
        self.assertEqual(dangerous, ("dangerous_function",))

        star = self.issue_codes(
            "SELECT * FROM section WHERE section.del_flag = 0"
        )
        self.assertEqual(star, ("star_projection",))

        count_star = self.issue_codes(
            "SELECT COUNT(*) AS n FROM section WHERE section.del_flag = 0"
        )
        self.assertEqual(count_star, ())

    def test_safety_requires_registered_soft_delete_predicate(self):
        codes = self.issue_codes("SELECT COUNT(*) AS n FROM section")

        self.assertEqual(codes, ("soft_delete_missing",))

    def test_cte_name_is_not_treated_as_a_physical_table(self):
        codes = self.issue_codes(
            "WITH filtered AS ("
            "SELECT id FROM section WHERE section.del_flag = 0"
            ") SELECT COUNT(*) AS n FROM filtered"
        )

        self.assertEqual(codes, ())

    def test_valid_section_count_passes_safety(self):
        codes = self.issue_codes(
            "SELECT COUNT(*) AS n FROM section WHERE section.del_flag = 0"
        )

        self.assertEqual(codes, ())


if __name__ == "__main__":
    unittest.main()
