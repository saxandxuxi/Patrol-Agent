import unittest
from unittest.mock import Mock, patch

from app.query.compiler import CompiledQuery, compile_query
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import (
    AnswerPackage,
    ExecutionObservation,
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecAmbiguity,
    QuerySpecError,
    UserDecision,
    ValueBinding,
    VerificationIssue,
    VerificationReport,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.mdl_sql import CompilerFailure, MdlSqlPipelineError
from app.query.service import (
    _answer_text,
    _default_post_calc_executor,
    _default_template_executor,
    _default_wren_executor,
    _execute_plan,
    _materialize_inputs,
    _observation_payload,
    _repair_failed_post_calc,
    _scope_key,
    _section_identity,
    _simple_spec_mdl,
    _terminal_sections,
    confirm_single_turn,
    start_single_turn,
)
from app.query.scope_facts import INSIDE, OUTSIDE, PARTIAL, ScopeFacts
from app.query.spec_mdl import UnsupportedQuerySpec
from app.services.llm import LLMError
from app.services.route_resolver import QueryPlanError
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
    @patch("app.query.service.empty_scope_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_count_outside_known_coverage_returns_zero_with_explicit_caveat(
        self,
        compile_query,
        execute_compiled,
        empty_scope_facts,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="collection_time",
                    op="between",
                    storage_value=("2026-10-15", "2026-10-16"),
                    display_value="2026年10月15日",
                    source="user_explicit",
                    resolver="date",
                    raw_text="2026年10月15日",
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="2026年10月15日怀化绕城高速检出多少处泛油",
        )
        compile_query.return_value = compiled_count_query()
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*) AS n FROM ai_problem",
            columns=("n",),
            row_count=1,
            preview_rows=({"n": "0"},),
            result_rows=({"n": "0"},),
            rows_fetched=1,
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=OUTSIDE,
            detail=(
                "怀化市绕城高速公路现有病害记录的采集时间在 "
                "2026-10-28 16:43 至 2026-11-04 10:37"
            ),
        )

        answer = _execute_plan(
            QueryPlan(
                question=spec.question,
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        section = answer.sections[0]
        self.assertEqual(section["status"], "accepted")
        self.assertIn("0处", section["answer"])
        self.assertIn("2026-10-28", section["answer"])
        self.assertIn("不能据此推断", section["answer"])
        self.assertIn("2026-10-28", section["coverage_detail"])

    @patch("app.query.service.empty_scope_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_distribution_outside_known_coverage_uses_same_scope_caveat(
        self,
        compile_query,
        execute_compiled,
        empty_scope_facts,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="sk-route",
                    display_value="上海-昆明",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="上海-昆明",
                ),
                ValueBinding(
                    field="pile_number",
                    op="between",
                    storage_value=(1180000, 1220000),
                    display_value="K1180+000 至 K1220+000",
                    source="user_explicit",
                    resolver="pile_range",
                    raw_text="K1180到K1220",
                ),
            ),
            output_projection=("problem_type_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="上海-昆明K1180到K1220按病害类型分别有多少处",
        )
        compile_query.return_value = compiled_count_query()
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT problem_type_name, COUNT(*) FROM ai_problem",
            columns=("problem_type_name", "count"),
            row_count=0,
            preview_rows=(),
            result_rows=(),
            rows_fetched=0,
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=OUTSIDE,
            detail=(
                "查询区间超出该路线桩号覆盖范围；"
                "上海-昆明现有病害记录的桩号在 "
                "K1256+462~K1430+687"
            ),
        )

        answer = _execute_plan(
            QueryPlan(
                question=spec.question,
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        section = answer.sections[0]
        self.assertEqual(section["status"], "accepted")
        self.assertIn("没有可返回的分组记录", section["answer"])
        self.assertIn("K1256+462~K1430+687", section["answer"])
        self.assertIn("不能据此推断", section["answer"])
        self.assertNotIn("无法给出数值", section["answer"])

    @patch("app.query.service.empty_scope_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_partial_pile_coverage_zero_is_not_reported_as_complete_zero(
        self,
        compile_query,
        execute_compiled,
        empty_scope_facts,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="bt-route",
                    display_value="包头-茂名",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="包头-茂名高速",
                ),
                ValueBinding(
                    field="pile_number",
                    op="between",
                    storage_value=(2200000, 2210000),
                    display_value="K2200+000 至 K2210+000",
                    source="user_explicit",
                    resolver="pile_range",
                    raw_text="K2200到K2210",
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="包头-茂名K2200到K2210有多少处病害",
        )
        compile_query.return_value = compiled_count_query()
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*) AS n FROM ai_problem",
            columns=("n",),
            row_count=1,
            preview_rows=({"n": "0"},),
            result_rows=({"n": "0"},),
            rows_fetched=1,
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=PARTIAL,
            detail=(
                "查询区间仅部分落在该路线的现有病害记录覆盖范围内；"
                "包头-茂名现有病害记录的桩号在 "
                "K2161+000~K2203+999。未覆盖部分不能判定为无病害"
            ),
        )

        answer = _execute_plan(
            QueryPlan(
                question=spec.question,
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        section = answer.sections[0]
        self.assertEqual(section["status"], "accepted")
        self.assertNotIn("failure_code", section)
        self.assertIn("重叠部分未观察到匹配记录", section["answer"])
        self.assertIn("不是整个查询区间的完整零结果", section["answer"])
        self.assertIn("未覆盖部分不能判定为无病害", section["answer"])
        self.assertNotIn("属于“范围内确实没有记录”", section["answer"])

    @patch("app.query.service.coverage_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_non_empty_pile_query_carries_coverage_evidence(
        self,
        compile_query,
        execute_compiled,
        coverage_facts,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="bt-route",
                    display_value="包头-茂名",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="包头-茂名高速",
                ),
                ValueBinding(
                    field="pile_number",
                    op="between",
                    storage_value=(2200000, 2210000),
                    display_value="K2200+000 至 K2210+000",
                    source="user_explicit",
                    resolver="pile_range",
                    raw_text="K2200到K2210",
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="包头-茂名K2200到K2210有多少处病害",
        )
        compile_query.return_value = compiled_count_query()
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*) AS n FROM ai_problem",
            columns=("n",),
            row_count=1,
            preview_rows=({"n": "98"},),
            result_rows=({"n": "98"},),
            rows_fetched=1,
        )
        coverage_facts.return_value = ScopeFacts(
            verdict=PARTIAL,
            detail=(
                "查询区间仅部分落在该路线的现有病害记录覆盖范围内；"
                "包头-茂名现有病害记录的桩号在 "
                "K2161+000~K2203+999。未覆盖部分不能判定为无病害，"
                "当前结果只能代表有记录覆盖的重叠部分"
            ),
        )

        answer = _execute_plan(
            QueryPlan(
                question=spec.question,
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        section = answer.sections[0]
        self.assertEqual(section["status"], "accepted")
        self.assertIn("K2161+000~K2203+999", section["coverage_detail"])
        self.assertIn("K2161+000~K2203+999", section["answer"])
        self.assertIn("仅部分", section["answer"])
        self.assertIn("未覆盖部分不能判定为无病害", section["answer"])
        self.assertIn("只能代表有记录覆盖的重叠部分", section["answer"])

    @patch("app.query.service.coverage_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_null_joined_route_is_rendered_as_unlinked_relation(
        self,
        compile_query,
        execute_compiled,
        coverage_facts,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("task_number", "route_name"),
            filters=(),
            output_projection=("linked_disease_count",),
            order_by=(),
            limit=500,
            required_assertions=(),
            question="列出巡检任务、路线和关联病害数",
        )
        compile_query.return_value = CompiledQuery(
            "SELECT task_number, route_name, linked_disease_count",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT task_number, route_name, linked_disease_count",
            columns=("任务编号", "路线名称", "任务关联病害数"),
            row_count=1,
            preview_rows=(
                {
                    "任务编号": "XJJL202611050000",
                    "路线名称": None,
                    "任务关联病害数": "0",
                },
            ),
            result_rows=(
                {
                    "任务编号": "XJJL202611050000",
                    "路线名称": None,
                    "任务关联病害数": "0",
                },
            ),
            rows_fetched=1,
        )
        coverage_facts.return_value = ScopeFacts(
            verdict=INSIDE,
            detail="巡检记录覆盖到 2026-11-05 09:53",
        )

        answer = _execute_plan(
            QueryPlan(
                question=spec.question,
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        self.assertIn("路线名称 未关联路线", answer.text)
        self.assertIn("任务关联病害数 0", answer.text)
        self.assertNotIn("路线名称 无有效数值", answer.text)

    @patch("app.query.service._default_post_calc_executor")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_completed_enum_zero_group_reaches_post_calc(
        self,
        compile_query,
        execute_compiled,
        post_calc_executor,
    ):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "route_length_km"),
            dimensions=("up_or_down",),
            filters=(
                ValueBinding(
                    field="up_or_down",
                    op="in",
                    storage_value=["1", "2"],
                    display_value="上行、下行",
                    source="user_explicit",
                    resolver="enum",
                    raw_text="上行和下行",
                ),
            ),
            output_projection=("up_or_down", "count", "route_length_km"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="统计上下行各自坑槽数量和路线长度",
        )
        post_calc_spec = QuerySpec(
            id="compare",
            kind="mysql",
            subject="disease",
            view="data",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("analysis_only",),
            question="比较上下行密度",
        )
        compile_query.return_value = CompiledQuery(
            "SELECT direction, count, route_length_km",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT direction, count, route_length_km",
            columns=("上下行", "数量", "路线长度"),
            row_count=1,
            preview_rows=(
                {"上下行": "上行", "数量": "4", "路线长度": "540.299"},
            ),
            result_rows=(
                {"上下行": "上行", "数量": "4", "路线长度": "540.299"},
            ),
            rows_fetched=1,
        )
        post_calc_executor.return_value = {
            "accepted": True,
            "reply": "已比较上下行数据。",
            "rows": [],
            "columns": [],
        }

        _execute_plan(
            QueryPlan(
                question="上行和下行哪一侧的坑槽病害更密集",
                nodes=(
                    PlanNode(id="q1", spec=source_spec),
                    PlanNode(
                        id="compare",
                        spec=post_calc_spec,
                        depends_on=("q1",),
                        node_type="post_calc",
                    ),
                ),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        upstream = post_calc_executor.call_args.args[1]["q1"]["rows"]
        down = next(row for row in upstream if row["上下行"] == "下行")
        self.assertEqual(down["数量"], 0)
        self.assertEqual(down["路线长度"], "540.299")

    @patch("app.query.service._default_post_calc_executor")
    @patch("app.query.service.empty_scope_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_post_calc_is_unsupported_when_upstream_partial_scope_is_empty(
        self,
        compile_query,
        execute_compiled,
        empty_scope_facts,
        post_calc_executor,
    ):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="route-1",
                    display_value="怀化市绕城高速公路",
                    source="user_explicit",
                    resolver="route_dictionary",
                ),
                ValueBinding(
                    field="collection_time",
                    op=">=",
                    storage_value="2026-11-01 00:00:00",
                    display_value="2026-11-01 00:00:00",
                    source="user_explicit",
                    resolver="time_range",
                ),
                ValueBinding(
                    field="collection_time",
                    op="<",
                    storage_value="2026-12-01 00:00:00",
                    display_value="2026-12-01 00:00:00",
                    source="user_explicit",
                    resolver="time_range",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计指定批次病害数量",
        )
        post_calc_spec = QuerySpec(
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
            required_assertions=("analysis_only",),
            question="根据前序结果计算主病害占比",
        )
        compile_query.return_value = CompiledQuery(
            "SELECT COUNT(*) AS n",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*) AS n",
            columns=("数量",),
            row_count=0,
            preview_rows=(),
            result_rows=(),
            rows_fetched=0,
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=PARTIAL,
            detail=(
                "查询区间仅部分落在怀化市绕城高速公路的现有病害记录"
                "覆盖范围内，现有记录采集到 2026-11-04 10:37"
            ),
        )

        answer = _execute_plan(
            QueryPlan(
                question="查询指定批次病害总数并计算主病害占比",
                nodes=(
                    PlanNode(id="q1", spec=source_spec),
                    PlanNode(
                        id="q2",
                        spec=post_calc_spec,
                        depends_on=("q1",),
                        node_type="post_calc",
                    ),
                ),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        sections = {section["node_id"]: section for section in answer.sections}
        self.assertEqual(sections["q1"]["status"], "accepted")
        self.assertEqual(sections["q2"]["status"], "unsupported")
        self.assertEqual(
            sections["q2"]["failure_code"],
            "dependency_empty_scope",
        )
        self.assertIn("未覆盖部分", sections["q2"]["answer"])
        post_calc_executor.assert_not_called()

    @patch("app.query.service.empty_scope_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    def test_empty_explicit_enum_filter_is_materialized_as_zero_group(
        self,
        compile_query,
        execute_compiled,
        empty_scope_facts,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "route_length_km"),
            dimensions=("up_or_down",),
            filters=(
                ValueBinding(
                    field="up_or_down",
                    op="=",
                    storage_value="2",
                    display_value="下行",
                    source="user_explicit",
                    resolver="enum",
                    raw_text="下行",
                ),
            ),
            output_projection=("up_or_down", "count", "route_length_km"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="统计下行坑槽数量和路线长度",
        )
        compile_query.return_value = CompiledQuery(
            "SELECT direction, count, route_length_km",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT direction, count, route_length_km",
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=INSIDE,
            detail="测试范围覆盖",
        )

        answer = _execute_plan(
            QueryPlan(
                question="上海-昆明高速下行坑槽多少处",
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        section = answer.sections[0]
        self.assertEqual(section["status"], "accepted")
        self.assertEqual(len(section["rows"]), 1)
        self.assertEqual(section["rows"][0]["上下行"], "下行")
        self.assertEqual(section["rows"][0]["数量"], 0)
        self.assertIsNone(section["rows"][0]["路线长度"])
        self.assertIn("下行", section["answer"])
        self.assertIn("0", section["answer"])

    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.empty_scope_facts")
    def test_scoped_empty_count_is_materialized_as_zero(
        self,
        empty_scope_facts,
        compile_query,
        execute_compiled,
    ):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="up_or_down",
                    op="=",
                    storage_value="2",
                    display_value="下行",
                    source="user_explicit",
                    resolver="enum",
                    raw_text="下行",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        compile_query.return_value = CompiledQuery(
            "SELECT COUNT(*) AS n",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*) AS n",
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=INSIDE,
            detail="测试范围覆盖",
        )

        answer = _execute_plan(
            QueryPlan(
                question="下行有多少处",
                nodes=(PlanNode(id="q1", spec=spec),),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        section = answer.sections[0]
        self.assertEqual(section["status"], "accepted")
        self.assertEqual(section["rows"], [{"数量": "0"}])
        self.assertIn("共 0处", section["answer"])

    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.empty_scope_facts")
    def test_empty_list_dependency_propagates_as_zero_count(
        self,
        empty_scope_facts,
        compile_query,
        execute_compiled,
    ):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("patrol_number",),
            filters=(
                ValueBinding(
                    field="device_code",
                    op="=",
                    storage_value="DEV-2046",
                    display_value="DEV-2046",
                    source="user_explicit",
                    resolver="device",
                ),
            ),
            output_projection=("patrol_number",),
            order_by=(),
            limit=500,
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
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="patrol_number",
                    to_field="patrol_number",
                ),
            ),
        )
        compile_query.return_value = CompiledQuery(
            "SELECT 1",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
        )
        empty_scope_facts.return_value = ScopeFacts(
            verdict=INSIDE,
            detail="测试范围覆盖",
        )

        answer = _execute_plan(
            QueryPlan(
                question="设备在指定时间的病害数",
                nodes=(
                    PlanNode(id="q1", spec=source_spec),
                    PlanNode(
                        id="q2",
                        spec=target_spec,
                        depends_on=("q1",),
                    ),
                ),
            ),
            UserDecision(token=""),
            registry=CapabilityRegistry.load(),
        )

        target = answer.sections[1]
        self.assertEqual(target["status"], "accepted")
        self.assertEqual(target["rows"], [{"数量": "0"}])
        self.assertIn("q1", target["answer"])
        self.assertEqual(compile_query.call_count, 1)

    @patch("app.query.service.analyze_single_turn")
    def test_unresolved_route_returns_structured_unsupported_answer(self, analyze):
        analyze.side_effect = QueryPlanError(
            "没有找到路线“S20外环高速”，请确认路线编码或名称。"
        )

        result = start_single_turn(
            "S20外环高速过去六个月病害面积趋势",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertIn("没有找到路线", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "unsupported")
        self.assertEqual(
            result.answer.sections[0]["failure_code"],
            "query_scope_not_found",
        )

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

    def test_count_observation_maps_metric_to_display_column(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        payload = _observation_payload(
            ExecutionObservation(
                node_id="q1",
                ok=True,
                engine="wren",
                sql="SELECT COUNT(*)",
                columns=("病害数量",),
                preview_rows=({"病害数量": "10"},),
                result_rows=({"病害数量": "10"},),
                rows_fetched=1,
            ),
            spec=spec,
            question="病害总数",
            answer="共 10 处",
        )

        self.assertEqual(
            payload["field_labels"],
            {"count": "病害数量"},
        )

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

    @patch(
        "app.query.service.compile_queryspec_to_physical",
        return_value="SELECT COUNT(*) AS n FROM ai_problem",
    )
    def test_wren_adapter_delegates_to_pipeline_with_plan_id(
        self,
        compile_pipeline,
    ):
        spec = count_plan().nodes[0].spec.__class__(
            **{
                **count_plan().nodes[0].spec.__dict__,
                "question": "泛油有多少处？",
            }
        )
        sql = _default_wren_executor(spec, plan_id="plan:q1")

        self.assertEqual(sql, "SELECT COUNT(*) AS n FROM ai_problem")
        compile_pipeline.assert_called_once_with(
            spec,
            plan_id="plan:q1",
        )

    @patch(
        "app.query.service.compile_queryspec_to_physical",
        side_effect=MdlSqlPipelineError(
            CompilerFailure(
                code="compiler_not_covered",
                stage="deterministic",
                evidence={"reason": "bad token"},
                contract_hash="contract-hash",
                mdl_sql="SELECT broken",
            )
        ),
    )
    def test_pipeline_failure_is_not_rewritten_by_service(
        self,
        _compile_pipeline,
    ):
        spec = count_plan().nodes[0].spec.__class__(
            **{**count_plan().nodes[0].spec.__dict__, "question": "泛油有多少处？"}
        )
        with self.assertRaises(MdlSqlPipelineError) as caught:
            _default_wren_executor(spec)

        self.assertEqual(
            caught.exception.failure.code,
            "compiler_not_covered",
        )

    def test_compile_query_passes_plan_id_to_wren_executor(self):
        spec = count_plan().nodes[0].spec
        executor = Mock(return_value="SELECT 1")

        compiled = compile_query(
            spec,
            UserDecision(token="", choices={}, overrides={}),
            candidate=None,
            template_executor=None,
            wren_executor=executor,
            plan_id="plan:q1",
        )

        executor.assert_called_once_with(spec, plan_id="plan:q1")
        self.assertEqual(compiled.sql, "SELECT 1")

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

    @patch("app.query.service.verify_result")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    def test_repairable_result_with_rows_attaches_review_gate(
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
        _verify.return_value = VerificationReport(
            status="repairable",
            issues=(
                VerificationIssue(
                    code="wrong_shape",
                    message="结果像聚合值，但问题要求明细",
                    node_id="q1",
                    stage="semantic",
                ),
            ),
        )

        result = start_single_turn(
            "把面积前五名和长度前五名的病害调出来",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertIsNotNone(result.answer.review_gate)
        self.assertEqual(result.answer.review_gate["nodes"][0]["node_id"], "q1")

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
        self.assertIn("缺少日期或时间桶", answer)

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

    def test_single_upstream_value_uses_equality_binding(self):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number",),
            order_by=(),
            limit=1,
            required_assertions=(),
        )
        target_spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="patrol_number",
                    to_field="patrol_number",
                    op="in",
                ),
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("任务编号",),
            preview_rows=({"任务编号": "P1"},),
            result_rows=({"任务编号": "P1"},),
            rows_fetched=1,
        )

        effective = _materialize_inputs(
            target_spec,
            {"q1": observation},
            {"q1": source_spec},
        )

        self.assertEqual(effective.filters[0].op, "=")
        self.assertEqual(effective.filters[0].storage_value, "P1")

    def test_row_index_dependency_selects_the_ordered_upstream_row(self):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number",),
            order_by=({"field": "collection_time", "direction": "desc"},),
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
                    from_field="patrol_number",
                    to_field="patrol_number",
                    op="=",
                    row_index=1,
                ),
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("任务编号",),
            preview_rows=(
                {"patrol_number": "P1"},
                {"patrol_number": "P2"},
            ),
            result_rows=(
                {"patrol_number": "P1"},
                {"patrol_number": "P2"},
            ),
            rows_fetched=2,
        )

        effective = _materialize_inputs(
            target_spec,
            {"q1": observation},
            {"q1": source_spec},
        )

        self.assertEqual(effective.filters[0].op, "=")
        self.assertEqual(effective.filters[0].storage_value, "P2")

    def test_scalar_dependency_rejects_multiple_values_without_row_index(self):
        source_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number",),
            order_by=({"field": "collection_time", "direction": "desc"},),
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
                    from_field="patrol_number",
                    to_field="patrol_number",
                    op="=",
                ),
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("任务编号",),
            preview_rows=(
                {"patrol_number": "P1"},
                {"patrol_number": "P2"},
            ),
            result_rows=(
                {"patrol_number": "P1"},
                {"patrol_number": "P2"},
            ),
            rows_fetched=2,
        )

        with self.assertRaises(UnsupportedQuerySpec) as raised:
            _materialize_inputs(
                target_spec,
                {"q1": observation},
                {"q1": source_spec},
            )

        self.assertIn("row_index", str(raised.exception))

    def test_route_name_dependency_keeps_display_view_filter(self):
        """上游结果给的是路线中文名（展示视图），过滤必须回到 route_name IN，
        不能拿中文名去和 route.id 比较。"""

        target_spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("route_name",),
            filters=(),
            output_projection=("route_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="认不出来的病害主要集中在哪条路上？",
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="route_name",
                    to_field="route_name",
                    op="in",
                ),
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("路线名称",),
            preview_rows=({"route_name": "长沙－芷江高速公路"},),
            result_rows=({"route_name": "长沙－芷江高速公路"},),
            rows_fetched=1,
        )
        route = {
            "id": "51ca5f84a0f811f1a6310242ac120002",
            "code": "S50",
            "short_name": "长沙－芷江高速公路",
            "full_name": "S50长芷高速",
        }

        with patch(
            "app.services.route_resolver._resolve_route",
            return_value=route,
        ):
            effective = _materialize_inputs(
                target_spec,
                {"q1": observation},
                {"q1": target_spec},
            )

        binding = effective.filters[0]
        self.assertEqual(binding.op, "in")
        self.assertEqual(binding.storage_value, ["长沙－芷江高速公路"])
        self.assertEqual(binding.source, "previous_result")
        sql = _simple_spec_mdl(effective)
        self.assertIn("disease.route_name IN ('长沙－芷江高速公路')", sql)
        self.assertNotIn("route.id", sql)

    def test_enum_dependency_resolves_label_to_storage_code(self):
        """上游分组结果是中文标签（上行），过滤要用存储 code（1）。"""

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
            question="上行的病害有多少处？",
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="up_or_down",
                    to_field="up_or_down",
                    op="in",
                ),
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("上下行方向",),
            preview_rows=({"up_or_down": "上行"},),
            result_rows=({"up_or_down": "上行"},),
            rows_fetched=1,
        )

        effective = _materialize_inputs(
            target_spec,
            {"q1": observation},
            {"q1": target_spec},
        )

        binding = effective.filters[0]
        self.assertEqual(binding.op, "in")
        self.assertEqual(binding.storage_value, "1")
        self.assertEqual(binding.display_value, "上行")
        self.assertIn("disease.up_or_down IN ('1')", _simple_spec_mdl(effective))

    def test_unresolvable_route_dependency_stops_with_reason(self):
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
            question="这条路上的病害有多少处？",
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="route_name",
                    to_field="route_name",
                    op="in",
                ),
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT 1",
            columns=("路线名称",),
            preview_rows=({"route_name": "未知路线"},),
            result_rows=({"route_name": "未知路线"},),
            rows_fetched=1,
        )

        with self.assertRaises(UnsupportedQuerySpec) as ctx:
            _materialize_inputs(
                target_spec,
                {"q1": observation},
                {"q1": target_spec},
            )

        self.assertIn("未知路线", str(ctx.exception))

    def _post_calc_plan(self, question="比较两次巡查", node_id="pc"):
        data_spec = QuerySpec(
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
            required_assertions=(),
            question="统计两次巡查的坑槽数量",
        )
        calc_spec = QuerySpec(
            id=node_id,
            kind="mysql",
            subject="disease",
            view="count",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="计算两次巡查的变化量",
        )
        plan = QueryPlan(
            question=question,
            nodes=(
                PlanNode(id="q1", spec=data_spec),
                PlanNode(id=node_id, spec=calc_spec, depends_on=("q1",)),
            ),
        )
        failed = AnswerPackage(
            "列计算必须且只能有一个多行来源节点",
            (
                {
                    "node_id": node_id,
                    "type": "post_calc",
                    "status": "repairable",
                    "failure_code": "calculation_operand_missing",
                    "answer": "列计算必须且只能有一个多行来源节点；请改用 calculate_series。",
                    "depends_on": ["q1"],
                },
            ),
        )
        return plan, failed

    @patch("app.query.service.resolve_templates")
    @patch("app.query.service._execute_plan")
    @patch("app.query.service.analyze_single_turn")
    def test_failed_post_calc_is_replanned_once(self, analyze, execute, templates):
        plan, failed = self._post_calc_plan()
        analyze.return_value = plan
        templates.return_value = empty_candidate_set()
        execute.return_value = AnswerPackage(
            "变化 -3",
            (
                {
                    "node_id": "pc",
                    "type": "post_calc",
                    "status": "accepted",
                    "answer": "变化 -3",
                },
            ),
        )

        result = _repair_failed_post_calc(
            plan,
            failed,
            "比较两次巡查",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            None,
            (),
        )

        self.assertEqual(result.text, "变化 -3")
        self.assertEqual(analyze.call_count, 1)
        self.assertIn("post_calc", analyze.call_args.kwargs["planning_feedback"])

    @patch("app.query.service.resolve_templates")
    @patch("app.query.service._execute_plan")
    @patch("app.query.service.analyze_single_turn")
    def test_post_calc_repair_keeps_original_when_data_scope_drifts(
        self,
        analyze,
        execute,
        templates,
    ):
        plan, failed = self._post_calc_plan()
        drifted_spec = plan.nodes[0].spec.__class__(
            **{
                **plan.nodes[0].spec.__dict__,
                "filters": (
                    ValueBinding(
                        field="problem_type_name",
                        op="=",
                        storage_value="泛油",
                        display_value="泛油",
                        source="user_explicit",
                        resolver="damage_type_dictionary",
                        raw_text="泛油",
                    ),
                ),
            }
        )
        analyze.return_value = QueryPlan(
            question=plan.question,
            nodes=(
                PlanNode(id="q1", spec=drifted_spec),
                plan.nodes[1],
            ),
        )

        result = _repair_failed_post_calc(
            plan,
            failed,
            "比较两次巡查",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            None,
            (),
        )

        self.assertEqual(result, failed)
        self.assertEqual(execute.call_count, 0)

    @patch("app.query.service.resolve_templates")
    @patch("app.query.service._execute_plan")
    @patch("app.query.service.analyze_single_turn")
    def test_post_calc_repair_keeps_original_when_repaired_answer_still_fails(
        self,
        analyze,
        execute,
        templates,
    ):
        plan, failed = self._post_calc_plan()
        analyze.return_value = plan
        templates.return_value = empty_candidate_set()
        execute.return_value = AnswerPackage(
            "还是算不出来",
            (
                {
                    "node_id": "pc",
                    "type": "post_calc",
                    "status": "repairable",
                    "failure_code": "calculation_operand_missing",
                    "answer": "还是算不出来",
                },
            ),
        )

        result = _repair_failed_post_calc(
            plan,
            failed,
            "比较两次巡查",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            None,
            (),
        )

        self.assertEqual(result, failed)

    @patch("app.query.service.analyze_single_turn")
    def test_successful_answer_never_triggers_replan(self, analyze):
        plan, _failed = self._post_calc_plan()
        accepted = AnswerPackage(
            "变化 -3",
            (
                {
                    "node_id": "pc",
                    "type": "post_calc",
                    "status": "accepted",
                    "answer": "变化 -3",
                },
            ),
        )

        result = _repair_failed_post_calc(
            plan,
            accepted,
            "比较两次巡查",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            None,
            (),
        )

        self.assertEqual(result, accepted)
        self.assertEqual(analyze.call_count, 0)

    @patch("app.query.service.resolve_templates")
    @patch("app.query.service._execute_plan")
    @patch("app.query.service.analyze_single_turn")
    def test_confirmed_template_path_also_repairs_failed_post_calc(
        self,
        analyze,
        execute,
        templates,
    ):
        """用户点过确认卡之后，计算节点失败同样要重排一次。

        以前 `confirm_single_turn` 直接 `_execute_plan` 就返回，修复循环只挂在
        没命中模板的分支上：确认路径（用户实际走得最多的一条）计算失败时，
        用户只能拿到"后置计算必须声明 calculation"这种内部错误。
        """
        plan, failed = self._post_calc_plan()
        accepted = AnswerPackage(
            "变化 -3",
            (
                {
                    "node_id": "pc",
                    "type": "post_calc",
                    "status": "accepted",
                    "answer": "变化 -3",
                },
            ),
        )
        analyze.return_value = plan
        templates.return_value = candidate_set_with_one_template()
        execute.side_effect = [failed, accepted]

        pending = start_single_turn(
            "比较两次巡查",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        result = confirm_single_turn(
            pending.confirmation_ticket.token,
            {"q1": "c1"},
            {},
        )

        self.assertEqual(result.text, "变化 -3")
        self.assertEqual(analyze.call_count, 2)
        self.assertIn("post_calc", analyze.call_args.kwargs["planning_feedback"])
        first_call, second_call = execute.call_args_list
        # 重排后仍沿用用户已确认的口径与候选集，不再弹第二次确认卡。
        self.assertIs(second_call.args[1], first_call.args[1])
        self.assertIs(second_call.args[2], pending.candidate_set)

    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=RuntimeError("model unavailable"),
    )
    def test_severity_post_calc_compares_density_and_reports_inputs(
        self,
        _chat,
    ):
        payload = _default_post_calc_executor(
            {
                "question": "哪个道路的病害最严重？",
                "depends_on": ["q1"],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": "divide",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q1", "field": "count"},
                        },
                        {
                            "name": "denominator",
                            "source": {
                                "node": "q1",
                                "field": "route_length_km",
                            },
                        },
                    ],
                    "output": {
                        "name": "disease_density",
                        "label": "病害密度",
                        "unit": "处/公里",
                    },
                    "precision": 4,
                },
            },
            {
                "q1": {
                    "columns": ["路线名称", "病害数量", "病害总面积", "路线长度"],
                    "field_labels": {
                        "route_name": "路线名称",
                        "count": "病害数量",
                        "total_area": "病害总面积",
                        "route_length_km": "路线长度",
                    },
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
        self.assertIn("B", payload["reply"])
        self.assertIn("病害密度", payload["reply"])
        self.assertIn("处/公里", payload["reply"])

    def test_trend_post_calc_reports_direction_from_upstream_rows(self):
        payload = _default_post_calc_executor(
            {
                "question": "长沙－芷江高速公路病害数量，最近是在增加还是在减少？",
                "depends_on": ["q1"],
                "calculation": {
                    "tool": "calculate_series",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "labels",
                            "source": {"node": "q1", "field": "月份"},
                        },
                        {
                            "name": "values",
                            "source": {"node": "q1", "field": "病害数量"},
                        },
                    ],
                    "precision": 6,
                },
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
                "calculation": {
                    "tool": "calculate_scalar",
                    "operation": "percentage_share",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q1_low", "field": "count"},
                        },
                        {
                            "name": "total",
                            "source": {"node": "q2_total", "field": "count"},
                        },
                    ],
                    "output": {
                        "name": "low_confidence_share",
                        "label": "低置信度占比",
                        "unit": "%",
                    },
                    "precision": 2,
                },
            },
            {
                "q1_low": {
                    "field_labels": {"count": "病害数量"},
                    "rows": [{"病害数量": "17623"}],
                },
                "q2_total": {
                    "field_labels": {"count": "病害数量"},
                    "rows": [{"病害数量": "34889"}],
                },
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

    @patch("app.query.service.compile_queryspec_to_physical", return_value="SELECT 1")
    @patch("app.services.nl2sql.build_template_sql_for")
    def test_builtin_template_uses_controlled_queryspec_compiler(
        self,
        build_sql,
        compile_queryspec,
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
        sql = _default_template_executor(
            candidate,
            {},
            count_plan().nodes[0].spec,
        )
        self.assertEqual(sql, "SELECT 1")
        compile_queryspec.assert_called_once()
        build_sql.assert_not_called()

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

    @patch("app.query.service.verify_result")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_detail_inherits_count_metric_instead_of_first_numeric_metric(
        self,
        analyze,
        resolve,
        compile_query_mock,
        execute,
        verify,
    ):
        base_plan = length_query_plan()
        detail_node = base_plan.nodes[1]
        summary_spec = QuerySpec(
            id="summary",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("sum_effective_disease_length", "count"),
            dimensions=(),
            filters=detail_node.spec.filters,
            output_projection=("sum_effective_disease_length", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        analyze.return_value = QueryPlan(
            question=base_plan.question,
            nodes=(
                PlanNode(id="summary", spec=summary_spec),
                detail_node,
            ),
        )
        resolve.return_value = empty_candidate_set()
        compile_query_mock.side_effect = [
            CompiledQuery(
                "SELECT SUM(effective_disease_length), COUNT(*) FROM ai_problem",
                "wren",
                "wren",
            ),
            CompiledQuery("SELECT id FROM ai_problem LIMIT 500", "wren", "wren"),
        ]
        execute.side_effect = [
            ExecutionObservation(
                node_id="summary",
                ok=True,
                engine="wren",
                sql="SELECT SUM(effective_disease_length), COUNT(*) FROM ai_problem",
                columns=("病害总长度", "病害数量"),
                row_count=1,
                preview_rows=(
                    {"病害总长度": "798.54", "病害数量": "158"},
                ),
                result_rows=(
                    {"病害总长度": "798.54", "病害数量": "158"},
                ),
                rows_fetched=1,
            ),
            ExecutionObservation(
                node_id="detail",
                ok=True,
                engine="wren",
                sql="SELECT id FROM ai_problem LIMIT 500",
                columns=("id",),
                row_count=158,
                preview_rows=({"id": "1"},),
                rows_fetched=158,
            ),
        ]
        verify.return_value = VerificationReport(status="accepted")

        result = start_single_turn(
            base_plan.question,
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        detail_section = result.answer.sections[-1]
        self.assertEqual(detail_section["total_count"], 158)
        self.assertFalse(detail_section["truncated"])
        self.assertIn("共 158 条，当前展示前 158 条", detail_section["answer"])

    @patch("app.query.service.verify_result", return_value=VerificationReport(status="accepted"))
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    def test_detail_does_not_inherit_smaller_inconsistent_count(
        self,
        analyze,
        _resolve,
        compile_query_mock,
        execute,
        _verify,
    ):
        analyze.return_value = length_query_plan()
        compile_query_mock.side_effect = [
            compiled_count_query(),
            CompiledQuery("SELECT id FROM ai_problem LIMIT 500", "wren", "wren"),
        ]
        execute.side_effect = [
            ExecutionObservation(
                node_id="summary",
                ok=True,
                engine="wren",
                sql="SELECT COUNT(*)",
                columns=("n",),
                row_count=1,
                preview_rows=({"n": "1"},),
                result_rows=({"n": "1"},),
                rows_fetched=1,
            ),
            ExecutionObservation(
                node_id="detail",
                ok=True,
                engine="wren",
                sql="SELECT id FROM ai_problem LIMIT 500",
                columns=("id",),
                row_count=2,
                preview_rows=({"id": "1"}, {"id": "2"}),
                result_rows=({"id": "1"}, {"id": "2"}),
                rows_fetched=2,
            ),
        ]

        result = start_single_turn(
            "病害长度大于 2 米的有多少处？把明细列出来",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        detail_section = result.answer.sections[-1]
        self.assertIsNone(detail_section["total_count"])
        self.assertFalse(detail_section["truncated"])
        self.assertIn("返回 2 条", detail_section["answer"])

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

    @patch("app.query.service.verify_result", return_value=VerificationReport(status="accepted"))
    @patch("app.query.service.execute_compiled", return_value=count_observation())
    @patch("app.query.service.compile_query", return_value=compiled_count_query())
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    @patch("app.query.service._default_post_calc_executor")
    def test_zero_denominator_post_calc_returns_structured_unsupported(
        self,
        post_calc,
        analyze,
        _resolve,
        _compile,
        _execute,
        _verify,
    ):
        data_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计任务数量",
        )
        calc_spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="patrol",
            view="avg",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="计算平均每次任务检出病害数",
        )
        analyze.return_value = QueryPlan(
            question="设备平均每次任务检出多少病害",
            nodes=(
                PlanNode(id="q1", spec=data_spec),
                PlanNode(
                    id="q2",
                    spec=calc_spec,
                    depends_on=("q1",),
                    node_type="post_calc",
                ),
            ),
        )
        post_calc.return_value = {
            "accepted": False,
            "reply": "除数不能为 0",
            "failure": {
                "failure_code": "calculation_zero_denominator",
                "reason": "除数不能为 0",
                "evidence": {
                    "calculation": {
                        "operation": "divide",
                        "zero_operand": "denominator",
                        "operands": [
                            {
                                "name": "denominator",
                                "label": "统计任务数量",
                                "value": 0,
                            }
                        ],
                        "output": {
                            "label": "平均每次任务检出病害数",
                        },
                    }
                },
            },
        }

        result = start_single_turn(
            "设备平均每次任务检出多少病害",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        section = result.answer.sections[-1]
        self.assertEqual(section["status"], "unsupported")
        self.assertEqual(
            section["failure_code"],
            "calculation_zero_denominator",
        )
        self.assertIn("统计任务数量为 0", result.answer.text)
        self.assertIn("平均值无法定义", result.answer.text)

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
        self.assertNotIn("换一种说法", result.answer.text)
        self.assertNotIn("切换模型", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "unsupported")

    @patch(
        "app.query.service.analyze_single_turn",
        side_effect=QuerySpecError(
            "未知能力缺口 id：missing_route_length_field；"
            "只能使用当前能力视图中登记的能力缺口"
        ),
    )
    def test_unknown_capability_gap_does_not_leak_internal_id(
        self,
        _analyze,
    ):
        result = start_single_turn(
            "按管养片区统计病害",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertNotIn("missing_route_length_field", result.answer.text)
        self.assertIn("能力缺口", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "unsupported")

    @patch(
        "app.query.service.analyze_single_turn",
        side_effect=QuerySpecError(
            "calculation_required: post_calc 必须声明 calculation 工具和参数"
        ),
    )
    def test_calculation_plan_failure_is_explained_without_internal_code(
        self,
        _analyze,
    ):
        """计算节点生不出来是系统侧计划问题，不能把内部 failure_code 甩给用户。"""
        result = start_single_turn(
            "上海到昆明和二连浩特到广州哪条泛油更多",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertIsNotNone(result.answer)
        self.assertNotIn("calculation_required", result.answer.text)
        self.assertIn("没能给出可执行的计算方案", result.answer.text)
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

    @patch(
        "app.query.service.analyze_single_turn",
        side_effect=LLMError(
            "HTTP 429: insufficient balance",
            category="quota_exhausted",
            status_code=429,
            retryable=False,
        ),
    )
    def test_model_quota_failure_keeps_infrastructure_code(self, _analyze):
        result = start_single_turn(
            "复杂问题",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertIsNotNone(result.answer)
        self.assertIn("额度不足", result.answer.text)
        self.assertEqual(result.answer.sections[0]["status"], "error")
        self.assertEqual(
            result.answer.sections[0]["failure_code"],
            "llm_quota_exhausted",
        )

    @patch("app.query.service._default_post_calc_executor")
    @patch("app.query.service.verify_result", return_value=VerificationReport(status="accepted"))
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn")
    def test_incomplete_trend_keeps_observed_series_for_downstream_calculation(
        self,
        analyze,
        _resolve,
        compile_query_mock,
        execute,
        _verify,
        post_calc,
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
        post_calc.return_value = {
            "accepted": True,
            "reply": "在已观察到的时间桶中，2026-11 最高。",
            "rows": [{"周": "2026-11", "数量": 1883}],
            "columns": ["周", "数量"],
        }

        result = start_single_turn(
            "最近三个月趋势",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertEqual(compile_query_mock.call_count, 1)
        self.assertEqual(len(result.answer.sections), 2)
        self.assertEqual(result.answer.sections[0]["status"], "accepted")
        self.assertEqual(result.answer.sections[1]["status"], "accepted")
        self.assertIn(
            "无法据此判断完整时间窗口的趋势",
            result.answer.text,
        )
        self.assertIn(
            "已观察到的时间桶",
            result.answer.text,
        )
        upstream = post_calc.call_args.args[1]
        self.assertEqual(
            upstream["q1"]["rows"],
            [
                {"月份": "2026-10", "数量": "11886"},
                {"月份": "2026-11", "数量": "1883"},
            ],
        )

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
