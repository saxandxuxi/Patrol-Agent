import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.audit import audit_mdl_sql
from app.query.mdl_sql.audit.ast import parse_mdl_sql
from app.query.mdl_sql.audit.safety_audit import audit_safety
from app.query.mdl_sql.audit.contract_audit import audit_contract
from app.query.mdl_sql.audit.relationship_audit import audit_relationships
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.query.mdl_sql.models import (
    MdlSqlCandidate,
    RelationshipSubgraph,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


def binding(field: str, value: object) -> ValueBinding:
    return binding_with_op(field, "=", value)


def binding_with_op(
    field: str,
    op: str,
    value: object,
) -> ValueBinding:
    return ValueBinding(
        field=field,
        op=op,
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


def disease_count_contract(
    registry,
    snapshot,
    *,
    metric: str = "count",
):
    spec = QuerySpec(
        id="disease-count",
        kind="mysql",
        subject="disease",
        view="count",
        metrics=(metric,),
        dimensions=(),
        filters=(binding("route_level", "0"),),
        output_projection=(metric,),
        order_by=(),
        limit=None,
        required_assertions=("preserve_user_scope",),
        question="统计国道路段病害数量",
    )
    return build_mdl_query_contract(
        spec,
        registry,
        snapshot,
        plan_id="plan-audit",
    )


def disease_distribution_contract(registry, snapshot):
    spec = QuerySpec(
        id="disease-distribution",
        kind="mysql",
        subject="disease",
        view="distribution",
        metrics=("count",),
        dimensions=("up_or_down",),
        filters=(),
        output_projection=("up_or_down", "count"),
        order_by=(),
        limit=None,
        required_assertions=("preserve_user_scope",),
        question="按上下行统计病害数量",
    )
    return build_mdl_query_contract(
        spec,
        registry,
        snapshot,
        plan_id="plan-audit",
    )


def section_top_contract(registry, snapshot):
    spec = QuerySpec(
        id="section-top",
        kind="mysql",
        subject="section",
        view="top_records",
        metrics=(),
        dimensions=(),
        filters=(),
        output_projection=("id", "route_name"),
        order_by=({"field": "id", "direction": "desc"},),
        limit=5,
        required_assertions=("preserve_user_scope",),
        question="列出路段前五条",
    )
    return build_mdl_query_contract(
        spec,
        registry,
        snapshot,
        plan_id="plan-audit",
    )


def route_list_contract(registry, snapshot):
    spec = QuerySpec(
        id="route-list",
        kind="mysql",
        subject="route",
        view="list",
        metrics=(),
        dimensions=(),
        filters=(),
        output_projection=("id",),
        order_by=(),
        limit=10,
        required_assertions=("preserve_user_scope",),
        question="列出路线",
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
        cls.disease_contract = disease_count_contract(
            cls.registry,
            cls.snapshot,
        )
        cls.disease_sql = (
            "SELECT COUNT(*) AS count "
            "FROM disease JOIN route ON disease.routes_id = route.id "
            "WHERE disease.del_flag = 0 AND route.level = '0'"
        )

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

    def test_contract_audit_enforces_root_subject(self):
        disease_codes = self.contract_codes(
            self.disease_sql,
            self.disease_contract,
        )
        self.assertEqual(disease_codes, ())

        wrong_root = self.contract_codes(
            self.disease_sql.replace("FROM disease", "FROM section"),
            self.disease_contract,
        )
        self.assertIn("root_subject_mismatch", wrong_root)

        section_codes = self.contract_codes(
            self.disease_sql,
            self.contract,
        )
        self.assertIn("root_subject_mismatch", section_codes)

    def test_contract_audit_enforces_filters_and_field_scope(self):
        missing = self.disease_sql.replace(
            " AND route.level = '0'",
            "",
        )
        self.assertIn("missing_filter", self.contract_codes(missing))

        wrong_value = self.disease_sql.replace(
            "route.level = '0'",
            "route.level = '1'",
        )
        self.assertIn(
            "filter_value_mismatch",
            self.contract_codes(wrong_value),
        )

        extra = self.disease_sql + " AND route.level >= '0'"
        self.assertIn("extra_filter", self.contract_codes(extra))

        unexpected_field = self.disease_sql + " AND disease.level = '0'"
        self.assertIn(
            "unexpected_field",
            self.contract_codes(unexpected_field),
        )

        unexpected_model = self.disease_sql.replace(
            "JOIN route ON disease.routes_id = route.id",
            "JOIN damage_dict "
            "ON disease.problem_base_id = damage_dict.id",
        )
        self.assertIn(
            "unexpected_model",
            self.contract_codes(unexpected_model),
        )

    def test_contract_audit_supports_registered_filter_ops(self):
        cases = (
            (
                "in",
                ("0", "1"),
                "route.level IN ('0', '1')",
            ),
            (
                "between",
                ("0", "1"),
                "route.level BETWEEN '0' AND '1'",
            ),
            (
                "is_null",
                None,
                "route.level IS NULL",
            ),
            (
                "not_null",
                None,
                "route.level IS NOT NULL",
            ),
        )
        for op, value, predicate in cases:
            with self.subTest(op=op):
                spec = QuerySpec(
                    id=f"disease-{op}",
                    kind="mysql",
                    subject="disease",
                    view="count",
                    metrics=("count",),
                    dimensions=(),
                    filters=(binding_with_op("route_level", op, value),),
                    output_projection=("count",),
                    order_by=(),
                    limit=None,
                    required_assertions=("preserve_user_scope",),
                    question="按路线等级筛选病害",
                )
                contract = build_mdl_query_contract(
                    spec,
                    self.registry,
                    self.snapshot,
                    plan_id="plan-audit",
                )
                sql = (
                    "SELECT COUNT(*) AS count "
                    "FROM disease JOIN route "
                    "ON disease.routes_id = route.id "
                    "WHERE disease.del_flag = 0 AND "
                    + predicate
                )
                self.assertEqual(
                    self.contract_codes(sql, contract),
                    (),
                )

    def test_contract_audit_accepts_half_open_time_range(self):
        spec = QuerySpec(
            id="patrol-between",
            kind="mysql",
            subject="patrol",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                binding_with_op(
                    "collection_time",
                    "between",
                    ("2026-11-01 00:00:00", "2026-11-02 00:00:00"),
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计十一月一日巡检次数",
        )
        contract = build_mdl_query_contract(
            spec,
            self.registry,
            self.snapshot,
            plan_id="plan-audit",
        )
        sql = (
            "SELECT COUNT(*) AS count FROM patrol "
            "WHERE patrol.del_flag = 0 "
            "AND patrol.create_time >= '2026-11-01 00:00:00' "
            "AND patrol.create_time < '2026-11-02 00:00:00'"
        )

        self.assertEqual(self.contract_codes(sql, contract), ())

    def test_relationship_audit_rejects_illegal_edges_and_field_pairs(self):
        route_edge = self.snapshot.relationships["disease_route"]
        disease_only = self.route_less_disease_contract()
        subgraph_with_unrelated_model = RelationshipSubgraph(
            root_model="disease",
            models=("disease", "section"),
            relationships=(route_edge,),
        )
        illegal_edge = (
            "SELECT COUNT(*) AS count FROM disease "
            "JOIN section ON disease.sections_id = section.id "
            "WHERE disease.del_flag = 0"
        )
        self.assertIn(
            "illegal_relationship",
            self.relationship_codes(
                illegal_edge,
                disease_only,
                subgraph_with_unrelated_model,
            ),
        )

        wrong_pair = (
            "SELECT COUNT(*) AS count "
            "FROM disease JOIN route ON disease.sections_id = route.id "
            "WHERE disease.del_flag = 0"
        )
        self.assertIn(
            "illegal_relationship",
            self.relationship_codes(
                wrong_pair,
                disease_only,
                RelationshipSubgraph(
                    root_model="disease",
                    models=("disease", "route"),
                    relationships=(route_edge,),
                ),
            ),
        )

        illegal_inside_cte = (
            "WITH filtered AS ("
            "SELECT disease.id FROM disease "
            "JOIN section ON disease.sections_id = section.id "
            "WHERE disease.del_flag = 0"
            ") SELECT COUNT(*) AS count FROM filtered"
        )
        self.assertIn(
            "illegal_relationship",
            self.relationship_codes(
                illegal_inside_cte,
                disease_only,
                subgraph_with_unrelated_model,
            ),
        )

    def test_relationship_audit_rejects_one_to_many_fanout(self):
        contract = route_list_contract(self.registry, self.snapshot)
        subgraph = RelationshipSubgraph(
            root_model="route",
            models=("route", "section"),
            relationships=(self.snapshot.relationships["section_route"],),
        )
        sql = (
            "SELECT route.id AS id "
            "FROM route JOIN section ON section.route_id = route.id "
            "WHERE route.del_flag = 0 LIMIT 10"
        )

        self.assertIn(
            "cardinality_risk",
            self.relationship_codes(sql, contract, subgraph),
        )

        safe_distinct = (
            "SELECT COUNT(DISTINCT route.id) AS count "
            "FROM route JOIN section ON section.route_id = route.id "
            "WHERE route.del_flag = 0"
        )
        self.assertNotIn(
            "cardinality_risk",
            self.relationship_codes(
                safe_distinct,
                contract,
                subgraph,
            ),
        )

    def test_contract_audit_enforces_grouped_view_shape(self):
        contract = disease_distribution_contract(
            self.registry,
            self.snapshot,
        )
        sql = (
            "SELECT disease.up_or_down AS up_or_down, "
            "COUNT(*) AS count FROM disease "
            "WHERE disease.del_flag = 0"
        )
        self.assertIn(
            "invalid_view_shape",
            self.contract_codes(sql, contract),
        )
        grouped = sql + " GROUP BY disease.up_or_down"
        self.assertEqual(self.contract_codes(grouped, contract), ())

        for view in ("trend", "top_groups"):
            with self.subTest(view=view):
                self.assertIn(
                    "invalid_view_shape",
                    self.contract_codes(
                        sql,
                        replace(contract, view=view),
                    ),
                )

    def test_contract_audit_rejects_aggregate_degraded_to_detail(self):
        sql = (
            "SELECT disease.id AS count FROM disease "
            "WHERE disease.del_flag = 0"
        )
        self.assertIn(
            "invalid_view_shape",
            self.contract_codes(sql, self.disease_contract),
        )

    def test_contract_audit_requires_outputs_and_exact_alias(self):
        missing = (
            "SELECT COUNT(*) FROM disease "
            "JOIN route ON disease.routes_id = route.id "
            "WHERE disease.del_flag = 0 AND route.level = '0'"
        )
        self.assertIn("missing_output", self.contract_codes(missing))

        wrong_alias = self.disease_sql.replace(
            "AS count",
            "AS total",
        )
        self.assertIn(
            "missing_output",
            self.contract_codes(wrong_alias),
        )

    def test_contract_audit_enforces_order_direction_and_limit(self):
        contract = section_top_contract(self.registry, self.snapshot)
        sql = (
            "SELECT section.id AS id, route.short_name AS route_name "
            "FROM section JOIN route ON section.route_id = route.id "
            "WHERE section.del_flag = 0 "
            "ORDER BY section.id DESC LIMIT 5"
        )
        self.assertEqual(self.contract_codes(sql, contract), ())

        no_order = sql.replace(" ORDER BY section.id DESC", "")
        self.assertIn(
            "order_required",
            self.contract_codes(no_order, contract),
        )

        wrong_direction = sql.replace("DESC", "ASC")
        self.assertIn(
            "invalid_view_shape",
            self.contract_codes(wrong_direction, contract),
        )

        enlarged = sql.replace("LIMIT 5", "LIMIT 6")
        self.assertIn(
            "invalid_limit",
            self.contract_codes(enlarged, contract),
        )

    def test_contract_audit_rejects_lost_count_distinct(self):
        contract = disease_count_contract(
            self.registry,
            self.snapshot,
            metric="distinct_problem_type_count",
        )
        sql = (
            "SELECT COUNT(disease.problem_type_name) "
            "AS distinct_problem_type_count "
            "FROM disease JOIN route "
            "ON disease.routes_id = route.id "
            "WHERE disease.del_flag = 0 AND route.level = '0'"
        )

        self.assertIn(
            "invalid_view_shape",
            self.contract_codes(sql, contract),
        )

    def test_contract_audit_accepts_top_per_outer_group_rank_shape(self):
        spec = QuerySpec(
            id="top-per-outer",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("up_or_down", "category_name"),
            filters=(),
            output_projection=("up_or_down", "category_name", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=3,
            required_assertions=(
                "preserve_user_scope",
                "top_per_outer_group",
            ),
            question="每个方向分别列出数量前三的病害类型",
        )
        contract = build_mdl_query_contract(
            spec,
            self.registry,
            self.snapshot,
            plan_id="plan-audit",
        )
        sql = (
            "WITH ranked AS ("
            "SELECT disease.up_or_down AS up_or_down, "
            "disease.category_name AS category_name, "
            "COUNT(*) AS count, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY disease.up_or_down "
            "ORDER BY COUNT(*) DESC"
            ") AS rank_in_outer "
            "FROM disease WHERE disease.del_flag = 0 "
            "GROUP BY disease.up_or_down, disease.category_name"
            ") SELECT up_or_down, category_name, count "
            "FROM ranked WHERE rank_in_outer <= 3"
        )

        self.assertEqual(self.contract_codes(sql, contract), ())

    def test_audit_mdl_sql_composes_all_auditors(self):
        candidate = MdlSqlCandidate(
            source="deterministic",
            mdl_sql=self.disease_sql,
            contract_id=self.disease_contract.contract_id,
            contract_hash=self.disease_contract.contract_hash,
        )
        subgraph = RelationshipSubgraph(
            root_model=self.disease_contract.root_model,
            models=self.disease_contract.allowed_models,
            relationships=tuple(
                self.snapshot.relationships[relationship_id]
                for relationship_id
                in self.disease_contract.required_relationship_ids
            ),
        )

        report = audit_mdl_sql(
            candidate,
            self.disease_contract,
            subgraph,
            self.snapshot,
            self.registry.snapshot.mdl_compilation,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.issues, ())
        self.assertEqual(report.root_model, "disease")
        self.assertEqual(report.used_relationships, ("disease_route",))

    def route_less_disease_contract(self):
        spec = QuerySpec(
            id="disease-countless",
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
            question="统计病害数量",
        )
        return build_mdl_query_contract(
            spec,
            self.registry,
            self.snapshot,
            plan_id="plan-audit",
        )

    def contract_codes(self, sql, contract=None):
        parsed = parse_mdl_sql(sql, "mysql")
        issues = audit_contract(
            parsed,
            contract or self.disease_contract,
        )
        return tuple(issue.code for issue in issues)

    def relationship_codes(self, sql, contract, subgraph):
        parsed = parse_mdl_sql(sql, "mysql")
        issues = audit_relationships(parsed, contract, subgraph)
        return tuple(issue.code for issue in issues)


if __name__ == "__main__":
    unittest.main()
