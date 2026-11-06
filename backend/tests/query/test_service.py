import unittest
from unittest.mock import patch

from app.query.compiler import CompiledQuery
from app.query.contracts import (
    ExecutionObservation,
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecAmbiguity,
    QuerySpecError,
    ValueBinding,
    VerificationReport,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import (
    _answer_text,
    _default_post_calc_executor,
    _default_template_executor,
    _default_wren_executor,
    _observation_payload,
    _scope_key,
    _section_identity,
    _simple_spec_mdl,
    _terminal_sections,
    start_single_turn,
)
from app.query.spec_mdl import UnsupportedQuerySpec
from app.services.llm import LLMError
from app.query.templates import (
    CandidateSet,
    ConfirmationCard,
    TemplateCandidate,
    TemplateContract,
)
from tests.query.factories import count_plan, detail_spec, length_query_plan


def candidate_set_with_one_template() -> CandidateSet:
    contract = TemplateContract(
        id="template-1",
        version=1,
        source="builtin",
        status="active",
        kind="mysql",
        adapter_id="builtin",
        native_id="template-1",
        subject="disease",
        view="count",
        metrics=("count",),
        dimensions=(),
        filters=(),
        fixed_conditions=(),
    )
    return CandidateSet(
        candidates=(
            TemplateCandidate(
                candidate_id="c1",
                node_id="q1",
                contract=contract,
                spec_fingerprint="fingerprint",
                bound_parameters=(),
                covered_constraints=("count",),
                fixed_conditions=(),
                warnings=(),
                requires_confirmation=True,
                confirmation_token="candidate-token",
                confirmation_card=ConfirmationCard(
                    source="builtin",
                    title="病害数量",
                    version=1,
                    scope_summary="主体=disease；视图=count",
                    bound_parameters=(),
                    fixed_conditions=(),
                    warnings=(),
                ),
            ),
        )
    )


def empty_candidate_set() -> CandidateSet:
    return CandidateSet()


def compiled_count_query() -> CompiledQuery:
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


class ServiceTests(unittest.TestCase):
    def test_section_identity_names_the_sub_question(self):
        spec = count_plan().nodes[0].spec
        node = PlanNode(
            id="q2_day",
            spec=QuerySpec(
                **{
                    **spec.__dict__,
                    "question": "查询长沙－芷江高速公路11月3日的病害分布",
                }
            ),
        )

        identity = _section_identity(node)

        self.assertEqual(identity["node_id"], "q2_day")
        self.assertIn("11月3日", identity["title"])
        self.assertEqual(
            identity["question"],
            "查询长沙－芷江高速公路11月3日的病害分布",
        )
        self.assertEqual(identity["tool"], "query_data")

    def test_empty_observation_keeps_logical_columns_for_dependencies(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(),
            output_projection=("problem_type_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        payload = _observation_payload(
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT 1",
                columns=(),
                rows_fetched=0,
            ),
            spec=spec,
            question="11月2日病害分布",
            answer="查询返回 0 行",
        )
        self.assertEqual(payload["columns"], ["problem_type_name", "count"])

    def test_terminal_sections_keep_only_final_calculation(self):
        plan = QueryPlan(
            question="对比",
            nodes=(
                PlanNode(id="q1", spec=count_plan().nodes[0].spec),
                PlanNode(id="q2", spec=count_plan().nodes[0].spec),
                PlanNode(
                    id="calc",
                    spec=count_plan().nodes[0].spec,
                    depends_on=("q1", "q2"),
                    node_type="post_calc",
                ),
            ),
        )
        sections = [
            {"node_id": "q1", "answer": "第一组", "status": "accepted"},
            {"node_id": "q2", "answer": "第二组", "status": "accepted"},
            {"node_id": "calc", "answer": "对比结论", "status": "accepted"},
        ]

        terminal = _terminal_sections(plan, sections)

        self.assertEqual(terminal, [sections[2]])

    @patch("app.query.service.wren_engine.compile_mdl_sql_verbose")
    @patch("app.query.service.sql_engine.generate_mdl_sql", return_value="MDL SQL")
    @patch("app.query.service.nl2sql.retrieve_query_context", return_value={})
    @patch("app.query.service.wren_engine.mdl_context", return_value="models")
    @patch("app.query.service.nl2sql.select_tables", return_value=set())
    @patch("app.query.service.nl2sql.simple_patrol_route_list_mdl", return_value="")
    @patch("app.query.service.nl2sql.simple_route_group_mdl", return_value="")
    @patch(
        "app.query.service.nl2sql.build_query_plan",
        return_value={
            "kind": "mysql",
            "slots": {},
            "subject": "disease",
            "required_conditions": [],
        },
    )
    def test_wren_adapter_compiles_from_frozen_question(
        self,
        _build_plan,
        _route_group,
        _patrol_list,
        _select_tables,
        _mdl_context,
        _retrieve,
        _generate,
        compile_sql,
    ):
        compile_sql.return_value = ("SELECT COUNT(*) AS n FROM ai_problem", "")
        sql = _default_wren_executor(
            count_plan().nodes[0].spec.__class__(
                **{
                    **count_plan().nodes[0].spec.__dict__,
                    "question": "泛油有多少处？",
                }
            )
        )
        self.assertEqual(sql, "SELECT COUNT(*) AS n FROM ai_problem")

    @patch("app.query.service.wren_engine.compile_mdl_sql_verbose")
    @patch("app.query.service.sql_engine.generate_mdl_sql")
    @patch("app.query.service.nl2sql.retrieve_query_context", return_value={})
    @patch("app.query.service.wren_engine.mdl_context", return_value="models")
    @patch("app.query.service.nl2sql.select_tables", return_value=set())
    @patch("app.query.service.nl2sql.simple_patrol_route_list_mdl", return_value="")
    @patch("app.query.service.nl2sql.simple_route_group_mdl", return_value="")
    @patch(
        "app.query.service.nl2sql.build_query_plan",
        return_value={
            "kind": "mysql",
            "slots": {},
            "subject": "disease",
            "required_conditions": [],
        },
    )
    def test_controlled_compile_failure_does_not_fall_back_to_llm(
        self,
        _build_plan,
        _route_group,
        _patrol_list,
        _select_tables,
        _mdl_context,
        _retrieve,
        generate_sql,
        compile_sql,
    ):
        generate_sql.side_effect = ["GOOD MDL"]
        compile_sql.side_effect = [("", "bad token")]
        spec = count_plan().nodes[0].spec.__class__(
            **{**count_plan().nodes[0].spec.__dict__, "question": "泛油有多少处？"}
        )
        with self.assertRaisesRegex(UnsupportedQuerySpec, "bad token"):
            _default_wren_executor(spec)
        generate_sql.assert_not_called()
        self.assertEqual(compile_sql.call_count, 1)

    def test_count_answer_uses_domain_unit(self):
        answer = _answer_text(
            count_plan().nodes[0].spec,
            count_observation(),
        )
        self.assertIn("20108", answer)
        self.assertIn("处", answer)

    def test_detail_answer_reports_display_limit(self):
        answer = _answer_text(
            detail_spec(),
            ExecutionObservation(
                node_id="detail",
                ok=True,
                engine="wren",
                sql="SELECT id FROM ai_problem LIMIT 500",
                columns=("id",),
                row_count=500,
                preview_rows=({"id": "1"},),
                rows_fetched=500,
                total_count=20108,
                truncated=True,
            ),
        )
        self.assertIn("20108", answer)
        self.assertIn("前 500 条", answer)

    def test_sum_answer_uses_length_unit_from_metric(self):
        answer = _answer_text(
            detail_spec(
                id="sum",
                view="sum",
                metrics=("sum_disease_length",),
                output_projection=(),
                limit=None,
            ),
            ExecutionObservation(
                node_id="sum",
                ok=True,
                engine="wren",
                sql="SELECT SUM(length)",
                columns=("总长度",),
                row_count=1,
                preview_rows=({"总长度": "135279.0200"},),
                rows_fetched=1,
            ),
        )
        self.assertIn("135279.02", answer)
        self.assertIn("米", answer)
        self.assertNotIn("处", answer)

    def test_distribution_answer_uses_complete_result_not_only_preview(self):
        answer = _answer_text(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("route_name",),
                filters=(),
                output_projection=("route_name", "count"),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
            ),
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT route_name, COUNT(*)",
                columns=("路线名称", "数量"),
                row_count=12,
                preview_rows=({"路线名称": "A", "数量": "1"},),
                result_rows=tuple(
                    {"路线名称": f"路线{index}", "数量": str(index)}
                    for index in range(12)
                ),
                rows_fetched=12,
            ),
        )
        self.assertIn("路线11", answer)

    @patch("app.query.service.verify_result")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    def test_answer_sections_expose_sql_and_engine_for_semantic_review(
        self,
        analyze,
        _resolve,
        _compile,
        _execute,
        _verify,
    ):
        analyze.return_value = count_plan()
        _compile.return_value = CompiledQuery(
            "SELECT COUNT(*) AS n FROM ai_problem",
            "wren",
            "wren",
        )
        _execute.return_value = count_observation()
        _verify.return_value = VerificationReport(status="accepted")

        result = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        section = result.answer.sections[0]
        self.assertEqual(section["sql"], "SELECT COUNT(*) AS n FROM ai_problem")
        self.assertEqual(section["engine"], "wren")

    def test_large_distribution_reports_hidden_rows(self):
        answer = _answer_text(
            QuerySpec(
                id="q1",
                kind="mysql",
                subject="disease",
                view="distribution",
                metrics=("count",),
                dimensions=("route_name",),
                filters=(),
                output_projection=("route_name", "count"),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
            ),
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT route_name, COUNT(*)",
                columns=("路线名称", "数量"),
                row_count=60,
                preview_rows=({"路线名称": "A", "数量": "1"},),
                result_rows=tuple(
                    {"路线名称": f"路线{index}", "数量": str(index)}
                    for index in range(60)
                ),
                rows_fetched=60,
            ),
        )
        self.assertIn("共 60 行", answer)
        self.assertIn("当前展示前 10 行", answer)

    def test_trend_reports_time_window_and_missing_months(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="trend",
            metrics=("count",),
            dimensions=("collection_time_month",),
            filters=(
                ValueBinding(
                    field="collection_time",
                    op="between",
                    storage_value=(
                        "2026-08-06 00:00:00",
                        "2026-11-06 00:00:00",
                    ),
                    display_value="最近三个月",
                    source="model_inference",
                    resolver="time_range",
                    raw_text="最近三个月",
                ),
            ),
            output_projection=("collection_time_month", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        answer = _answer_text(
            spec,
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT month, COUNT(*)",
                columns=("月份", "数量"),
                row_count=2,
                preview_rows=(
                    {"月份": "2026-10", "数量": "11886"},
                    {"月份": "2026-11", "数量": "1883"},
                ),
                result_rows=(
                    {"月份": "2026-10", "数量": "11886"},
                    {"月份": "2026-11", "数量": "1883"},
                ),
                rows_fetched=2,
            ),
        )
        self.assertIn("查询窗口", answer)
        self.assertIn("未出现的月份", answer)

    def test_month_distribution_reports_months_without_rows(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("count",),
            dimensions=("month",),
            filters=(
                ValueBinding(
                    field="create_time",
                    op=">=",
                    storage_value="2026-01-01 00:00:00",
                    display_value="2026-01-01 00:00:00",
                    source="model_inference",
                    resolver="time_range",
                    raw_text="2026年",
                ),
                ValueBinding(
                    field="create_time",
                    op="<",
                    storage_value="2027-01-01 00:00:00",
                    display_value="2027-01-01 00:00:00",
                    source="model_inference",
                    resolver="time_range",
                    raw_text="2026年",
                ),
            ),
            output_projection=("month", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        answer = _answer_text(
            spec,
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT month, COUNT(*)",
                columns=("月份", "数量"),
                row_count=2,
                preview_rows=(
                    {"月份": "2026-10", "数量": "3"},
                    {"月份": "2026-11", "数量": "4"},
                ),
                result_rows=(
                    {"月份": "2026-10", "数量": "3"},
                    {"月份": "2026-11", "数量": "4"},
                ),
                rows_fetched=2,
            ),
        )
        self.assertIn("2026-01", answer)
        self.assertIn("2026-09", answer)
        self.assertIn("2026-12", answer)
        self.assertIn("没有匹配记录", answer)

    def test_scope_key_accepts_list_valued_filters(self):
        spec = count_plan().nodes[0].spec.__class__(
            **{
                **count_plan().nodes[0].spec.__dict__,
                "filters": (
                    ValueBinding(
                        field="problem_type_name",
                        op="in",
                        storage_value=["纵向裂缝", "横向裂缝"],
                        display_value="裂缝",
                        source="user_explicit",
                        resolver="damage_type_dictionary_expand",
                        raw_text="裂缝",
                    ),
                ),
            }
        )
        self.assertIsInstance(hash(_scope_key(spec)), int)

    @patch("app.query.service.verify_result", return_value=VerificationReport(status="accepted"))
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    def test_dependent_query_uses_materialized_upstream_values(
        self,
        analyze,
        _resolve,
        compile_query_mock,
        execute,
        _verify,
    ):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("id",),
            filters=(),
            output_projection=("id",),
            order_by=(),
            limit=2,
            required_assertions=(),
        )
        target_spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="id",
                    to_field="patrol_id",
                    op="in",
                ),
            ),
        )
        analyze.return_value = QueryPlan(
            question="依赖查询",
            nodes=(
                PlanNode(id="q1", spec=source_spec),
                PlanNode(
                    id="q2",
                    spec=target_spec,
                    depends_on=("q1",),
                ),
            ),
        )
        compile_query_mock.side_effect = [
            CompiledQuery("SELECT id FROM patrol", "wren", "wren"),
            CompiledQuery("SELECT COUNT(*) FROM disease", "wren", "wren"),
        ]
        execute.side_effect = [
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT id FROM patrol",
                columns=("巡检任务ID",),
                row_count=2,
                preview_rows=(
                    {"巡检任务ID": "p1"},
                    {"巡检任务ID": "p2"},
                ),
                result_rows=(
                    {"巡检任务ID": "p1"},
                    {"巡检任务ID": "p2"},
                ),
                rows_fetched=2,
            ),
            ExecutionObservation(
                node_id="q2",
                ok=True,
                engine="wren",
                sql="SELECT COUNT(*) FROM disease",
                columns=("count",),
                row_count=1,
                preview_rows=({"count": "7"},),
                result_rows=({"count": "7"},),
                rows_fetched=1,
            ),
        ]

        start_single_turn(
            "依赖查询",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        effective = compile_query_mock.call_args_list[1].args[0]
        self.assertEqual(
            effective.filters[0].storage_value,
            ["p1", "p2"],
        )
        self.assertEqual(effective.filters[0].source, "previous_result")

    def test_severity_post_calc_compares_density_and_reports_inputs(self):
        payload = _default_post_calc_executor(
            {
                "question": "哪个道路的病害最严重？",
                "depends_on": ["q1"],
            },
            {
                "q1": {
                    "columns": ["路线名称", "病害数量", "病害总面积", "路线长度"],
                    "rows": [
                        {
                            "路线名称": "A",
                            "病害数量": "100",
                            "病害总面积": "1000",
                            "路线长度": "100",
                        },
                        {
                            "路线名称": "B",
                            "病害数量": "80",
                            "病害总面积": "2000",
                            "路线长度": "40",
                        },
                    ],
                }
            },
        )
        self.assertTrue(payload["accepted"])
        self.assertIn("密度最高的是B", payload["reply"])
        self.assertIn("处/公里", payload["reply"])

    def test_trend_post_calc_reports_direction_from_upstream_rows(self):
        payload = _default_post_calc_executor(
            {
                "question": "长沙－芷江高速公路病害数量，最近是在增加还是在减少？",
                "depends_on": ["q1"],
            },
            {
                "q1": {
                    "columns": ["月份", "病害数量"],
                    "rows": [
                        {"月份": "2026-10", "病害数量": "11886"},
                        {"月份": "2026-11", "病害数量": "1883"},
                    ],
                }
            },
        )
        self.assertTrue(payload["accepted"])
        self.assertIn("下降", payload["reply"])

    def test_ai_reliability_post_calc_reports_count_share_and_basis(self):
        payload = _default_post_calc_executor(
            {
                "question": "AI识别的病害类型，靠谱吗？有多少是认不出来的？",
                "depends_on": ["q1_low", "q2_total"],
            },
            {
                "q1_low": {"rows": [{"count": "17623"}]},
                "q2_total": {"rows": [{"count": "34889"}]},
            },
        )
        self.assertTrue(payload["accepted"])
        self.assertIn("34889", payload["reply"])
        self.assertIn("17623", payload["reply"])
        self.assertIn("低置信度", payload["reply"])

    def test_severity_spec_compiles_all_required_metrics(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "total_area", "route_length_km"),
            dimensions=("route_name",),
            filters=(),
            output_projection=("route_name", "count", "total_area", "route_length_km"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="哪个道路的病害最严重？",
        )
        sql = _simple_spec_mdl(spec)
        self.assertIsNotNone(sql)
        self.assertIn("COUNT(*)", sql)
        self.assertIn("SUM(disease.area)", sql)
        self.assertIn("route.route_length_km", sql)
        self.assertNotIn("LIMIT 1", sql)

    @patch("app.query.service.nl2sql.build_template_sql_for", return_value="SELECT 1")
    @patch("app.query.service.nl2sql.build_query_plan", return_value={
        "kind": "mysql",
        "subject": "disease",
        "slots": {},
        "required_conditions": [],
    })
    def test_builtin_template_uses_registered_builder(
        self,
        _build_plan,
        build_sql,
    ):
        contract = TemplateContract(
            id="threshold_count",
            version=1,
            source="builtin",
            status="active",
            kind="mysql",
            adapter_id="builtin",
            native_id="threshold_count",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            fixed_conditions=(),
        )
        candidate = TemplateCandidate(
            candidate_id="c1",
            node_id="q1",
            contract=contract,
            spec_fingerprint="fp",
            bound_parameters=(),
            covered_constraints=(),
            fixed_conditions=(),
            warnings=(),
            requires_confirmation=True,
            confirmation_token="token",
            confirmation_card=ConfirmationCard(
                source="builtin",
                title="threshold",
                version=1,
                scope_summary="",
                bound_parameters=(),
                fixed_conditions=(),
                warnings=(),
            ),
        )
        sql = _default_template_executor(candidate, {}, count_plan().nodes[0].spec)
        self.assertEqual(sql, "SELECT 1")
        build_sql.assert_called_once()
        self.assertEqual(
            build_sql.call_args.args[1],
            "_build_threshold_count_template",
        )

    def test_route_trend_compiles_route_filter_and_month_bucket(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="trend",
            metrics=("count",),
            dimensions=("collection_time",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="route-1",
                    display_value="长沙－芷江高速公路",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="长沙－芷江高速公路",
                ),
            ),
            output_projection=("collection_time", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="长沙－芷江高速公路病害数量趋势",
        )
        sql = _simple_spec_mdl(spec)
        self.assertIsNotNone(sql)
        self.assertIn("route.id = 'route-1'", sql)
        self.assertIn("DATE_FORMAT(disease.collection_time, '%Y-%m')", sql)

    @patch("app.query.service.verify_result")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_detail_inherits_exact_count_from_same_scope_summary(
        self,
        analyze,
        resolve,
        compile_query_mock,
        execute,
        verify,
    ):
        analyze.return_value = length_query_plan()
        resolve.return_value = empty_candidate_set()
        compile_query_mock.side_effect = [
            compiled_count_query(),
            CompiledQuery("SELECT id FROM ai_problem LIMIT 500", "wren", "wren"),
        ]
        execute.side_effect = [
            count_observation(),
            ExecutionObservation(
                node_id="detail",
                ok=True,
                engine="wren",
                sql="SELECT id FROM ai_problem LIMIT 500",
                columns=("id",),
                row_count=500,
                preview_rows=({"id": "1"},),
                rows_fetched=500,
            ),
        ]
        verify.return_value = VerificationReport(status="accepted")

        result = start_single_turn(
            "病害长度大于 2 米的有多少处？把明细列出来",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        detail_section = result.answer.sections[-1]
        self.assertEqual(detail_section["total_count"], 20108)
        self.assertTrue(detail_section["truncated"])
        self.assertIn("共 20108 条，当前展示前 500 条", detail_section["answer"])

    @patch("app.query.service.verify_result", return_value=VerificationReport(status="accepted"))
    @patch("app.query.service.execute_compiled", return_value=count_observation())
    @patch("app.query.service.compile_query", return_value=compiled_count_query())
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    @patch("app.query.service._default_post_calc_executor")
    def test_post_calc_uses_upstream_rows_without_sql_compile(
        self,
        post_calc,
        analyze,
        _resolve,
        compile_query_mock,
        _execute,
        _verify,
    ):
        group_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("route_name", "problem_type_name"),
            filters=(),
            output_projection=("route_name", "problem_type_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计各路线下各病害类型数量",
        )
        top_spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="top",
            metrics=("count",),
            dimensions=("route_name", "problem_type_name"),
            filters=(),
            output_projection=("route_name", "problem_type_name", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=1,
            required_assertions=("preserve_user_scope",),
            question="基于q1的结果，找出每条路线数量最多的主要病害类型",
        )
        analyze.return_value = QueryPlan(
            question="找出每条路线数量最多的主要病害类型",
            nodes=(
                PlanNode(id="q1", spec=group_spec),
                PlanNode(
                    id="q2",
                    spec=top_spec,
                    depends_on=("q1",),
                    node_type="post_calc",
                ),
            ),
        )
        _execute.return_value = ExecutionObservation(
            **{
                **count_observation().__dict__,
                "columns": ("route_name", "problem_type_name", "count"),
                "result_rows": (
                    {"route_name": "A", "problem_type_name": "泛油", "count": 5},
                    {"route_name": "A", "problem_type_name": "裂缝", "count": 9},
                ),
                "preview_rows": (
                    {"route_name": "A", "problem_type_name": "泛油", "count": 5},
                    {"route_name": "A", "problem_type_name": "裂缝", "count": 9},
                ),
                "rows_fetched": 2,
            }
        )
        post_calc.return_value = {
            "accepted": True,
            "reply": "A 路线主要病害是裂缝（9处）。",
            "columns": (),
            "rows": (),
        }

        result = start_single_turn(
            "找出每条路线数量最多的主要病害类型",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertEqual(compile_query_mock.call_count, 1)
        self.assertEqual(post_calc.call_count, 1)
        task, upstream = post_calc.call_args.args
        self.assertEqual(task["depends_on"], ["q1"])
        self.assertEqual(len(upstream["q1"]["rows"]), 2)
        self.assertIn("裂缝", result.answer.text)

    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_template_match_returns_cards_without_execution(self, analyze, resolve):
        analyze.return_value = count_plan()
        resolve.return_value = candidate_set_with_one_template()
        result = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.candidate_set)
        self.assertIsNone(result.answer)

    @patch(
        "app.query.service.analyze_single_turn",
        side_effect=QuerySpecError("规划不完整"),
    )
    def test_analysis_failure_returns_explicit_unsupported_answer(self, _analyze):
        result = start_single_turn(
            "复杂问题",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.answer)
        self.assertIn("规划不完整", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "unsupported")

    @patch(
        "app.query.service.analyze_single_turn",
        side_effect=QuerySpecAmbiguity("最近的时间范围未定义"),
    )
    def test_planning_ambiguity_returns_clear_request_for_scope(self, _analyze):
        result = start_single_turn(
            "最近的任务里哪次里程最高？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.answer)
        self.assertIn("补充明确口径", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "ambiguous")

    @patch(
        "app.query.service.analyze_single_turn",
        side_effect=LLMError("模型没有返回合法 JSON"),
    )
    def test_model_json_failure_returns_structured_error(self, _analyze):
        result = start_single_turn(
            "复杂问题",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.answer)
        self.assertIn("重试或切换模型", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "error")
        self.assertEqual(
            result.answer.sections[0]["failure_code"],
            "llm_output_invalid",
        )

    @patch("app.query.service.verify_result", return_value=VerificationReport(status="accepted"))
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    def test_incomplete_trend_stops_downstream_conclusion(
        self,
        analyze,
        _resolve,
        compile_query_mock,
        execute,
        _verify,
    ):
        trend_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="trend",
            metrics=("count",),
            dimensions=("collection_time_month",),
            filters=(
                ValueBinding(
                    field="collection_time",
                    op="between",
                    storage_value=(
                        "2026-08-06 00:00:00",
                        "2026-11-06 00:00:00",
                    ),
                    display_value="最近三个月",
                    source="model_inference",
                    resolver="time_range",
                    raw_text="最近三个月",
                ),
            ),
            output_projection=("collection_time_month", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        calculation = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="data",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="判断趋势",
        )
        analyze.return_value = QueryPlan(
            question="判断趋势",
            nodes=(
                PlanNode(id="q1", spec=trend_spec),
                PlanNode(
                    id="q2",
                    spec=calculation,
                    depends_on=("q1",),
                    node_type="post_calc",
                ),
            ),
        )
        compile_query_mock.return_value = CompiledQuery(
            "SELECT month, COUNT(*)",
            "wren",
            "wren",
        )
        execute.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT month, COUNT(*)",
            columns=("月份", "数量"),
            row_count=2,
            preview_rows=(
                {"月份": "2026-10", "数量": "11886"},
                {"月份": "2026-11", "数量": "1883"},
            ),
            result_rows=(
                {"月份": "2026-10", "数量": "11886"},
                {"月份": "2026-11", "数量": "1883"},
            ),
            rows_fetched=2,
        )

        result = start_single_turn(
            "最近三个月趋势",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertEqual(compile_query_mock.call_count, 1)
        self.assertEqual(len(result.answer.sections), 1)
        self.assertEqual(result.answer.sections[0]["status"], "unsupported")
        self.assertIn("时间序列不完整", result.answer.text)

    @patch("app.query.service.execute_compiled", return_value=count_observation())
    @patch("app.query.service.compile_query", return_value=compiled_count_query())
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_no_template_can_continue_to_wren(
        self,
        analyze,
        resolve,
        _compile_query,
        _execute,
    ):
        analyze.return_value = count_plan()
        resolve.return_value = empty_candidate_set()
        result = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.answer)
