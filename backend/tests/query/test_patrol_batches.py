import unittest
from unittest.mock import patch

from app.query.contracts import (
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    ValueBinding,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.patrol_batches import (
    PatrolBatchResolutionError,
    describe_batch_lookup_evidence,
    resolve_entity_comparison_batches,
    validate_explicit_patrol_batches,
)


ROUTE_ID = "51ca5f84a0f811f1a6310242ac120002"


def _date_filter(value: str, op: str) -> ValueBinding:
    return ValueBinding(
        field="collection_time",
        op=op,
        storage_value=value,
        display_value=value[:10],
        source="user_explicit",
        resolver="time_window",
        raw_text=value,
    )


def _data_node(node_id: str, start: str, end: str) -> PlanNode:
    return PlanNode(
        id=node_id,
        spec=QuerySpec(
            id=node_id,
            kind="mysql",
            subject="disease",
            view="list",
            metrics=(),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value=ROUTE_ID,
                    display_value="长沙－芷江高速公路",
                    source="user_explicit",
                    resolver="route_resolver",
                ),
                ValueBinding(
                    field="problem_type_name",
                    op="=",
                    storage_value="坑槽",
                    display_value="坑槽",
                    source="user_explicit",
                    resolver="enum_dictionary",
                ),
                _date_filter(start, ">="),
                _date_filter(end, "<"),
            ),
            output_projection=("problem_number", "pile_number"),
            order_by=(),
            limit=500,
            required_assertions=("preserve_user_scope",),
        ),
    )


def _comparison_plan() -> QueryPlan:
    return QueryPlan(
        question=(
            "长沙－芷江高速在2026年10月28日和2026年11月2日这两次巡检中，"
            "后一次比前一次新增了多少处坑槽病害？"
        ),
        nodes=(
            _data_node("before", "2026-10-28", "2026-10-29"),
            _data_node("after", "2026-11-02", "2026-11-03"),
            PlanNode(
                id="compare",
                spec=QuerySpec(
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
                    required_assertions=("dataset_comparison",),
                ),
                depends_on=("before", "after"),
                node_type="post_calc",
            ),
        ),
    )


def _explicit_batch_plan(batch_number: str) -> QueryPlan:
    return QueryPlan(
        question=f"2026年11月，上海-昆明高速巡检批次 {batch_number} 的病害分布",
        nodes=(
            PlanNode(
                id="q1",
                spec=QuerySpec(
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
                            storage_value=ROUTE_ID,
                            display_value="上海-昆明",
                            source="user_explicit",
                            resolver="route_resolver",
                        ),
                        ValueBinding(
                            field="patrol_number",
                            op="=",
                            storage_value=batch_number,
                            display_value=batch_number,
                            source="user_explicit",
                            resolver="identity:patrol_number",
                        ),
                        _date_filter("2026-11-01 00:00:00", ">="),
                        _date_filter("2026-12-01 00:00:00", "<"),
                    ),
                    output_projection=("problem_type_name", "count"),
                    order_by=({"field": "count", "direction": "desc"},),
                    limit=3,
                    required_assertions=("preserve_user_scope",),
                ),
            ),
        ),
    )


class PatrolBatchResolutionTests(unittest.TestCase):
    def test_missing_explicit_patrol_number_is_not_treated_as_coverage_gap(self):
        batch_number = "XCBH202611050001"

        with self.assertRaises(PatrolBatchResolutionError) as raised:
            validate_explicit_patrol_batches(
                _explicit_batch_plan(batch_number),
                query=lambda _kind, _sql: {"rows": []},
            )

        self.assertEqual(raised.exception.status, "unsupported")
        self.assertIn(batch_number, str(raised.exception))
        self.assertIn("巡检任务台账中不存在", str(raised.exception))
        evidence = raised.exception.blocked_evidence["q1"]
        self.assertEqual(
            evidence["lookup_scope"],
            "explicit_patrol_identity_lookup",
        )
        self.assertEqual(evidence["requested_identifiers"], [batch_number])
        self.assertEqual(evidence["matched_batch_count"], 0)

    def test_existing_explicit_patrol_number_keeps_the_original_plan(self):
        batch_number = "XCBH202611040003"
        plan = _explicit_batch_plan(batch_number)

        resolved = validate_explicit_patrol_batches(
            plan,
            query=lambda _kind, _sql: {
                "rows": [
                    {
                        "id": "batch-1",
                        "task_number": "XJJL202611040003",
                        "patrol_number": batch_number,
                        "create_time": "2026-11-04 09:00:00",
                    }
                ]
            },
        )

        self.assertEqual(resolved, plan)

    def test_explicit_identity_evidence_is_not_rendered_as_route_window_lookup(self):
        description = describe_batch_lookup_evidence(
            {
                "lookup_scope": "explicit_patrol_identity_lookup",
                "requested_identifiers": ["XCBH-MISSING"],
                "matched_batch_count": 0,
            }
        )

        self.assertEqual(description, "")

    def test_recent_window_bindings_do_not_get_re_resolved_as_dates(self):
        route = ValueBinding(
            field="route_name",
            op="=",
            storage_value=ROUTE_ID,
            display_value="长沙－芷江高速公路",
            source="user_explicit",
            resolver="route_resolver",
        )

        def disease_node(node_id: str, row_index: int) -> PlanNode:
            return PlanNode(
                id=node_id,
                spec=QuerySpec(
                    id=node_id,
                    kind="mysql",
                    subject="disease",
                    view="list",
                    metrics=(),
                    dimensions=(),
                    filters=(route,),
                    output_projection=("problem_number",),
                    order_by=(),
                    limit=500,
                    required_assertions=("preserve_user_scope",),
                    input_bindings=(
                        InputBinding(
                            from_node="recent_patrol_batches",
                            from_field="patrol_number",
                            to_field="patrol_number",
                            op="=",
                            row_index=row_index,
                        ),
                    ),
                ),
                depends_on=("recent_patrol_batches",),
            )

        plan = QueryPlan(
            question="比较最近两次巡检之间的新增病害",
            nodes=(
                PlanNode(
                    id="recent_patrol_batches",
                    spec=QuerySpec(
                        id="recent_patrol_batches",
                        kind="mysql",
                        subject="patrol",
                        view="list",
                        metrics=(),
                        dimensions=("patrol_number",),
                        filters=(route,),
                        output_projection=("patrol_number", "create_time"),
                        order_by=({"field": "create_time", "direction": "desc"},),
                        limit=2,
                        required_assertions=("recent_window",),
                    ),
                ),
                disease_node("latest", 0),
                disease_node("previous", 1),
                PlanNode(
                    id="compare",
                    spec=QuerySpec(
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
                        required_assertions=("dataset_comparison",),
                        comparison_depends_on=("latest", "previous"),
                    ),
                    depends_on=("latest", "previous"),
                    node_type="post_calc",
                ),
            ),
        )

        def unexpected_query(_kind, _sql):
            raise AssertionError("已绑定巡检批次的节点不应再次按日期查库")

        resolved = resolve_entity_comparison_batches(
            plan,
            query=unexpected_query,
        )

        self.assertEqual(resolved, plan)

    def test_missing_named_batch_is_an_explicit_resolution_error(self):
        def empty_query(_kind, _sql):
            return {"rows": []}

        with self.assertRaises(PatrolBatchResolutionError) as raised:
            resolve_entity_comparison_batches(
                _comparison_plan(),
                query=empty_query,
            )

        self.assertIn("2026-10-28", str(raised.exception))
        self.assertIn("巡检批次", str(raised.exception))
        self.assertEqual(raised.exception.status, "unsupported")
        evidence = raised.exception.blocked_evidence["before"]
        self.assertEqual(
            evidence["route_display_name"],
            "长沙－芷江高速公路",
        )
        self.assertEqual(evidence["route_id"], ROUTE_ID)
        self.assertEqual(
            evidence["requested_start"],
            "2026-10-28 00:00:00",
        )
        self.assertEqual(
            evidence["requested_end"],
            "2026-10-29 00:00:00",
        )
        self.assertEqual(evidence["resolved_batch_count"], 0)
        self.assertEqual(evidence["resolved_batches"], [])

    def test_each_date_is_rewritten_to_the_real_batch_id(self):
        def query(_kind, sql):
            if "2026-10-28 00:00:00" in sql:
                return {
                    "rows": [
                        {
                            "id": "batch-before",
                            "task_number": "XJJL-BEFORE",
                            "patrol_number": "XCBH-BEFORE",
                            "create_time": "2026-10-28 09:00:00",
                        }
                    ]
                }
            return {
                "rows": [
                    {
                        "id": "batch-after",
                        "task_number": "XJJL-AFTER",
                        "patrol_number": "XCBH-AFTER",
                        "create_time": "2026-11-02 09:00:00",
                    }
                ]
            }

        resolved = resolve_entity_comparison_batches(
            _comparison_plan(),
            query=query,
        )
        before, after, _compare = resolved.nodes

        self.assertIn(
            ("patrol_id", "batch-before"),
            {
                (item.field, str(item.storage_value))
                for item in before.spec.filters
            },
        )
        self.assertIn(
            ("patrol_id", "batch-after"),
            {
                (item.field, str(item.storage_value))
                for item in after.spec.filters
            },
        )
        self.assertFalse(
            any(
                item.field in {"collection_time", "create_time"}
                for item in (*before.spec.filters, *after.spec.filters)
            )
        )
        self.assertEqual(
            before.spec.output_projection,
            ("problem_number", "pile_number"),
        )

    def test_multiple_batches_on_same_date_require_disambiguation(self):
        def query(_kind, _sql):
            return {
                "rows": [
                    {
                        "id": "batch-1",
                        "task_number": "XJJL-1",
                        "patrol_number": "XCBH-1",
                        "create_time": "2026-10-28 09:00:00",
                    },
                    {
                        "id": "batch-2",
                        "task_number": "XJJL-2",
                        "patrol_number": "XCBH-2",
                        "create_time": "2026-10-28 15:00:00",
                    },
                ]
            }

        with self.assertRaises(PatrolBatchResolutionError) as raised:
            resolve_entity_comparison_batches(
                _comparison_plan(),
                query=query,
            )

        self.assertIn("多个巡检批次", str(raised.exception))

    @patch("app.query.service.resolve_entity_comparison_batches")
    def test_execution_stops_before_sql_when_batch_resolution_fails(
        self,
        resolve_batches,
    ):
        from app.query.contracts import UserDecision
        from app.query.service import _execute_plan

        resolve_batches.side_effect = PatrolBatchResolutionError(
            "路线在 2026-10-28 没有可用巡检批次"
        )

        answer = _execute_plan(
            _comparison_plan(),
            UserDecision(token=""),
        )

        self.assertIn("2026-10-28", answer.text)
        self.assertEqual(answer.sections[0]["status"], "ambiguous")
        self.assertEqual(
            answer.sections[0]["failure_code"],
            "patrol_batch_not_resolved",
        )

    @patch("app.query.service.coverage_facts")
    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_entity_comparison_batches")
    def test_missing_one_date_keeps_other_independent_result(
        self,
        resolve_batches,
        compile_query,
        execute_compiled,
        coverage_facts,
    ):
        from app.query.compiler import CompiledQuery
        from app.query.contracts import ExecutionObservation, UserDecision
        from app.query.scope_facts import INSIDE, ScopeFacts
        from app.query.service import _execute_plan

        plan = _comparison_plan()
        resolve_batches.side_effect = PatrolBatchResolutionError(
            "路线“长沙－芷江高速公路”在 2026-11-03 没有对应的巡检批次",
            plan=plan,
            blocked_nodes={
                "after": (
                    "路线“长沙－芷江高速公路”在 2026-11-03 的现有巡检"
                    "任务与路线关联台账中未核实到对应巡检批次，"
                    "不能把空批次当作 0。"
                )
            },
            status="unsupported",
            blocked_node_statuses={"after": "unsupported"},
            blocked_evidence={
                "after": {
                    "route_display_name": "长沙－芷江高速公路",
                    "route_id": ROUTE_ID,
                    "requested_start": "2026-11-03 00:00:00",
                    "requested_end": "2026-11-04 00:00:00",
                    "lookup_scope": "patrol_route_association_batch_lookup",
                    "resolved_batch_count": 0,
                    "resolved_batches": [],
                }
            },
        )
        compile_query.return_value = CompiledQuery(
            "SELECT problem_number, pile_number FROM ai_problem",
            "wren",
            "wren",
        )
        execute_compiled.return_value = ExecutionObservation(
            node_id="before",
            ok=True,
            engine="wren",
            sql="SELECT problem_number, pile_number FROM ai_problem",
            columns=("problem_number", "pile_number"),
            row_count=1,
            preview_rows=(
                {"problem_number": "TZ-1", "pile_number": "K10+000"},
            ),
            result_rows=(
                {"problem_number": "TZ-1", "pile_number": "K10+000"},
            ),
            rows_fetched=1,
        )
        coverage_facts.return_value = ScopeFacts(
            verdict=INSIDE,
            detail="查询范围在台账覆盖内",
        )

        answer = _execute_plan(plan, UserDecision(token=""))

        self.assertEqual(execute_compiled.call_count, 1)
        by_id = {section.get("node_id"): section for section in answer.sections}
        self.assertEqual(by_id["before"]["status"], "accepted")
        self.assertEqual(by_id["after"]["status"], "unsupported")
        self.assertEqual(by_id["compare"]["status"], "blocked")
        self.assertEqual(
            by_id["after"]["evidence"]["resolved_batch_count"],
            0,
        )
        self.assertEqual(
            by_id["after"]["evidence"]["route_id"],
            ROUTE_ID,
        )
        self.assertIn("TZ-1", answer.text)
        self.assertIn("2026-11-03", answer.text)
        self.assertIn("未核实到对应巡检批次", answer.text)
        self.assertEqual(answer.text.count("未核实到对应巡检批次"), 1)
        self.assertEqual(
            by_id["compare"]["evidence"]["dependency_ids"],
            ["after"],
        )
        self.assertIn("详细原因见对应上游步骤", by_id["compare"]["answer"])


if __name__ == "__main__":
    unittest.main()
