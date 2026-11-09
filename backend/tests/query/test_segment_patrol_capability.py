import unittest
from unittest.mock import patch

from app.calculations.models import (
    CalculationOperand,
    CalculationOutput,
    CalculationRequest,
)
from app.query.analysis import (
    _analyze_once,
    _normalize_terminal_answer_subject,
    _plan_subject_issue,
)
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import (
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    ValueBinding,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.patrol_windows import complete_recent_window_plan
from app.query.spec_mdl import compile_spec_mdl


class SegmentPatrolCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_partial_patrol_pile_scope_exposes_derived_fields(self):
        view = self.registry.planner_view("patrol", FieldReachSet())

        self.assertEqual(view.capability_gaps["patrol_pile_scope"]["status"], "partial")
        self.assertIn("病害记录", view.capability_gaps["patrol_pile_scope"]["reason"])
        self.assertEqual(
            view.capability_gaps["inspection_coverage_scope"]["status"],
            "partial",
        )
        self.assertIn("pile_number", view.fields)
        self.assertIn("pile_distance", view.fields)

    def test_patrol_segment_batch_compiles_through_disease_join(self):
        sql = compile_spec_mdl(
            QuerySpec(
                id="batches",
                kind="mysql",
                subject="patrol",
                view="list",
                metrics=(),
                dimensions=("patrol_number",),
                filters=(
                    ValueBinding(
                        field="route_name",
                        op="=",
                        storage_value="51ca5f84a0f811f1a6310242ac120002",
                        display_value="长沙－芷江高速公路",
                        source="user_explicit",
                        resolver="route_dictionary",
                    ),
                    ValueBinding(
                        field="pile_number",
                        op="between",
                        storage_value=("K50", "K51"),
                        display_value="K50至K51",
                        source="user_explicit",
                        resolver="pile_range",
                    ),
                ),
                output_projection=("patrol_number", "create_time"),
                order_by=({"field": "create_time", "direction": "desc"},),
                limit=2,
                required_assertions=("preserve_user_scope", "recent_window"),
            )
        )

        self.assertIsNotNone(sql)
        self.assertIn("LEFT JOIN disease", sql)
        self.assertIn("disease.pile_distance BETWEEN 50000 AND 51000", sql)

    def test_recent_window_keeps_route_and_pile_scope_for_batch_axis(self):
        plan = QueryPlan(
            question="长沙－芷江高速K50路段最近两次巡检检出多少处病害",
            nodes=(
                PlanNode(
                    id="counts",
                    spec=QuerySpec(
                        id="counts",
                        kind="mysql",
                        subject="disease",
                        view="distribution",
                        metrics=("count",),
                        dimensions=("patrol_number",),
                        filters=(
                            ValueBinding(
                                field="route_name",
                                op="=",
                                storage_value="51ca5f84a0f811f1a6310242ac120002",
                                display_value="长沙－芷江高速公路",
                                source="user_explicit",
                                resolver="route_dictionary",
                            ),
                            ValueBinding(
                                field="pile_number",
                                op="between",
                                storage_value=("K50", "K51"),
                                display_value="K50至K51",
                                source="user_explicit",
                                resolver="pile_range",
                            ),
                        ),
                        output_projection=("patrol_number", "count"),
                        order_by=(),
                        limit=2,
                        required_assertions=("preserve_user_scope", "recent_window"),
                    ),
                ),
            ),
        )

        completed = complete_recent_window_plan(plan, self.registry)
        batches = completed.nodes[0]

        self.assertEqual(batches.id, "recent_patrol_batches")
        self.assertEqual(
            {
                (item.field, item.storage_value)
                for item in batches.spec.filters
            },
            {
                ("route_name", "51ca5f84a0f811f1a6310242ac120002"),
                ("pile_number", ("K50", "K51")),
            },
        )

    def test_downstream_row_bindings_follow_the_inserted_patrol_batch_axis(self):
        target = PlanNode(
            id="recent_groups",
            spec=QuerySpec(
                id="recent_groups",
                kind="mysql",
                subject="disease",
                view="top_groups",
                metrics=("count",),
                dimensions=("patrol_number",),
                filters=(),
                output_projection=(),
                order_by=({"field": "collection_time", "direction": "desc"},),
                limit=2,
                required_assertions=("preserve_user_scope", "recent_window"),
            ),
        )

        def detail(node_id: str, row_index: int) -> PlanNode:
            return PlanNode(
                id=node_id,
                spec=QuerySpec(
                    id=node_id,
                    kind="mysql",
                    subject="disease",
                    view="list",
                    metrics=(),
                    dimensions=(),
                    filters=(),
                    output_projection=("problem_number",),
                    order_by=(),
                    limit=500,
                    required_assertions=("preserve_user_scope",),
                    input_bindings=(
                        InputBinding(
                            from_node="recent_groups",
                            from_field="patrol_number",
                            to_field="patrol_number",
                            op="=",
                            row_index=row_index,
                        ),
                    ),
                ),
                depends_on=("recent_groups",),
            )

        completed = complete_recent_window_plan(
            QueryPlan(
                question="比较最近两次巡检的病害",
                nodes=(target, detail("latest", 0), detail("previous", 1)),
            ),
            self.registry,
        )
        batch = next(
            node for node in completed.nodes
            if node.id == "recent_patrol_batches"
        )
        latest = next(node for node in completed.nodes if node.id == "latest")
        previous = next(
            node for node in completed.nodes if node.id == "previous"
        )

        self.assertTrue(batch.spec.order_by)
        self.assertEqual(
            batch.spec.dimensions,
            ("patrol_number", "create_time"),
        )
        for node in (latest, previous):
            self.assertEqual(
                node.spec.input_bindings[0].from_node,
                batch.id,
            )
            self.assertEqual(node.spec.input_bindings[0].from_field, "patrol_number")
            self.assertIn(batch.id, node.depends_on)

    def test_datetime_interval_reads_time_from_the_inserted_batch_axis(self):
        target = PlanNode(
            id="recent_groups",
            spec=QuerySpec(
                id="recent_groups",
                kind="mysql",
                subject="disease",
                view="top_groups",
                metrics=("count",),
                dimensions=("patrol_number",),
                filters=(),
                output_projection=(),
                order_by=({"field": "collection_time", "direction": "desc"},),
                limit=2,
                required_assertions=("preserve_user_scope", "recent_window"),
            ),
        )
        interval = PlanNode(
            id="interval",
            spec=QuerySpec(
                id="interval",
                kind="mysql",
                subject="patrol",
                view="detail",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=(),
                order_by=(),
                limit=None,
                required_assertions=("preserve_user_scope",),
                calculation=CalculationRequest(
                    tool="calculate_datetime_interval",
                    operation="elapsed_days",
                    operands=(
                        CalculationOperand(
                            name="earlier",
                            source={
                                "node": "recent_groups",
                                "field": "collection_time",
                                "row_index": 1,
                            },
                        ),
                        CalculationOperand(
                            name="later",
                            source={
                                "node": "recent_groups",
                                "field": "collection_time",
                                "row_index": 0,
                            },
                        ),
                    ),
                    output=CalculationOutput(
                        name="elapsed_days",
                        label="巡检间隔",
                        unit="天",
                    ),
                    precision=0,
                ),
            ),
            depends_on=("recent_groups",),
            node_type="post_calc",
        )

        completed = complete_recent_window_plan(
            QueryPlan(
                question="最近两次巡检相隔几天",
                nodes=(target, interval),
            ),
            self.registry,
        )
        result = next(
            node for node in completed.nodes if node.id == "interval"
        )
        self.assertNotIn(
            "recent_groups",
            {node.id for node in completed.nodes},
        )
        sources = {
            item.name: item.source
            for item in result.spec.calculation.operands
        }

        self.assertEqual(
            sources["earlier"],
            {
                "node": "recent_patrol_batches",
                "field": "create_time",
                "row_index": 1,
            },
        )
        self.assertEqual(
            sources["later"],
            {
                "node": "recent_patrol_batches",
                "field": "create_time",
                "row_index": 0,
            },
        )
        self.assertIn("recent_patrol_batches", result.depends_on)

    def test_mixed_dependencies_use_the_final_patrol_answer_subject(self):
        plan = QueryPlan(
            question="比较最近两次巡检任务并查看期间新增病害",
            nodes=(
                PlanNode(
                    id="batches",
                    spec=QuerySpec(
                        id="batches",
                        kind="mysql",
                        subject="patrol",
                        view="list",
                        metrics=(),
                        dimensions=("patrol_number",),
                        filters=(),
                        output_projection=("patrol_number", "create_time"),
                        order_by=({"field": "create_time", "direction": "desc"},),
                        limit=2,
                        required_assertions=("recent_window",),
                    ),
                ),
                PlanNode(
                    id="diseases",
                    spec=QuerySpec(
                        id="diseases",
                        kind="mysql",
                        subject="disease",
                        view="list",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=("problem_number",),
                        order_by=(),
                        limit=500,
                        required_assertions=("preserve_user_scope",),
                    ),
                ),
                PlanNode(
                    id="answer",
                    spec=QuerySpec(
                        id="answer",
                        kind="mysql",
                        subject="patrol",
                        view="data",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=(),
                        order_by=(),
                        limit=None,
                        required_assertions=("analysis_only",),
                    ),
                    depends_on=("batches", "diseases"),
                    node_type="post_calc",
                ),
            ),
        )

        self.assertEqual(_plan_subject_issue(plan, "patrol"), "")

    def test_dependent_terminal_subject_is_allowed_when_it_consumes_the_answer_subject(self):
        plan = QueryPlan(
            question="把最近一次巡查记录以及检测出来的病害调出来",
            nodes=(
                PlanNode(
                    id="q1",
                    spec=QuerySpec(
                        id="q1",
                        kind="mysql",
                        subject="patrol",
                        view="list",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=("patrol_number", "create_time"),
                        order_by=(
                            {"field": "create_time", "direction": "desc"},
                        ),
                        limit=1,
                        required_assertions=(
                            "recent_window",
                            "preserve_user_scope",
                        ),
                    ),
                ),
                PlanNode(
                    id="q2",
                    spec=QuerySpec(
                        id="q2",
                        kind="mysql",
                        subject="disease",
                        view="list",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=("problem_number",),
                        order_by=(
                            {
                                "field": "collection_time",
                                "direction": "desc",
                            },
                        ),
                        limit=500,
                        required_assertions=("preserve_user_scope",),
                    ),
                    depends_on=("q1",),
                ),
            ),
        )

        self.assertEqual(_plan_subject_issue(plan, "patrol"), "")

    def test_unrelated_terminal_subject_is_still_rejected(self):
        plan = QueryPlan(
            question="把最近一次巡查记录调出来",
            nodes=(
                PlanNode(
                    id="q1",
                    spec=QuerySpec(
                        id="q1",
                        kind="mysql",
                        subject="patrol",
                        view="list",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=("patrol_number",),
                        order_by=(
                            {"field": "create_time", "direction": "desc"},
                        ),
                        limit=1,
                        required_assertions=("recent_window",),
                    ),
                ),
                PlanNode(
                    id="q2",
                    spec=QuerySpec(
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
                    ),
                ),
            ),
        )

        self.assertIn("主体 disease", _plan_subject_issue(plan, "patrol"))

    def test_terminal_post_calc_inherits_the_determined_answer_subject(self):
        plan = QueryPlan(
            question="比较最近两次巡检任务并查看期间新增病害",
            nodes=(
                PlanNode(
                    id="batches",
                    spec=QuerySpec(
                        id="batches",
                        kind="mysql",
                        subject="patrol",
                        view="list",
                        metrics=(),
                        dimensions=("patrol_number",),
                        filters=(),
                        output_projection=("patrol_number", "create_time"),
                        order_by=({"field": "create_time", "direction": "desc"},),
                        limit=2,
                        required_assertions=("recent_window",),
                    ),
                ),
                PlanNode(
                    id="diseases",
                    spec=QuerySpec(
                        id="diseases",
                        kind="mysql",
                        subject="disease",
                        view="list",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=("problem_number",),
                        order_by=(),
                        limit=500,
                        required_assertions=("preserve_user_scope",),
                    ),
                ),
                PlanNode(
                    id="answer",
                    spec=QuerySpec(
                        id="answer",
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
                    ),
                    depends_on=("batches", "diseases"),
                    node_type="post_calc",
                ),
            ),
        )

        normalized = _normalize_terminal_answer_subject(plan, "patrol")

        self.assertEqual(normalized.nodes[-1].spec.subject, "patrol")
        self.assertEqual(
            normalized.nodes[-1].spec.required_assertions,
            ("analysis_only",),
        )

    @patch("app.query.analysis.chat_json")
    def test_partial_capability_gap_does_not_stop_a_query(self, chat_json):
        chat_json.return_value = {
            "final_goal": "查询病害数量",
            "intent_family": "count",
            "answer_operation": "single_result",
            "unknown_terms": ["capability_gap:patrol_pile_scope"],
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计病害数量",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }

        plan = _analyze_once(
            "统计病害数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.view, "count")


if __name__ == "__main__":
    unittest.main()
