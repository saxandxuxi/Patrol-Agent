import unittest
from dataclasses import replace

from app.query.contracts import (
    ExecutionObservation,
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    ValueBinding,
)
from app.query.patrol_windows import (
    complete_recent_window_groups,
    complete_recent_window_plan,
)


def _recent_plan() -> QueryPlan:
    return QueryPlan(
        question="最近五次巡查，网裂的数量是不是在增加？",
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
                            field="problem_type_name",
                            op="=",
                            storage_value="网状裂缝",
                            display_value="网裂",
                            source="user_explicit",
                            resolver="damage_type_dictionary",
                        ),
                    ),
                    output_projection=("patrol_number", "count"),
                    order_by=(),
                    limit=5,
                    required_assertions=(
                        "preserve_user_scope",
                        "recent_window",
                    ),
                    question="统计最近五次巡查中的网状裂缝数量",
                ),
            ),
        ),
        response_mode="analysis",
    )


class RecentPatrolWindowPlanTests(unittest.TestCase):
    def test_disease_collection_time_is_not_used_as_the_patrol_batch_axis(self):
        original = _recent_plan()
        target = original.nodes[0]
        plan = replace(
            original,
            nodes=(
                replace(
                    target,
                    spec=replace(
                        target.spec,
                        dimensions=("patrol_number", "collection_time"),
                        output_projection=(
                            "patrol_number",
                            "collection_time",
                            "count",
                        ),
                        order_by=(
                            {
                                "field": "collection_time",
                                "direction": "asc",
                            },
                        ),
                    ),
                ),
            ),
        )

        _batches, counts = complete_recent_window_plan(plan).nodes

        self.assertEqual(counts.spec.dimensions, ("patrol_number",))
        self.assertEqual(
            counts.spec.output_projection,
            ("patrol_number", "count"),
        )
        self.assertEqual(counts.spec.order_by, ())

    def test_existing_batch_node_is_normalized_before_dependent_counts(self):
        plan = QueryPlan(
            question="最近五次巡查里，坑槽数量是怎么变化的？",
            nodes=(
                PlanNode(
                    id="batches",
                    spec=QuerySpec(
                        id="batches",
                        kind="mysql",
                        subject="patrol",
                        view="list",
                        metrics=(),
                        dimensions=(
                            "patrol_number",
                            "create_time",
                            "route_name",
                        ),
                        filters=(),
                        output_projection=(
                            "patrol_number",
                            "create_time",
                            "route_name",
                        ),
                        order_by=(
                            {"field": "create_time", "direction": "desc"},
                        ),
                        limit=5,
                        required_assertions=(
                            "preserve_user_scope",
                            "recent_window",
                        ),
                        question="按巡检时间倒序取最近五次批次明细",
                    ),
                ),
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
                                field="problem_type_name",
                                op="=",
                                storage_value="坑槽",
                                display_value="坑槽",
                                source="user_explicit",
                                resolver="damage_type_dictionary",
                            ),
                        ),
                        output_projection=("patrol_number", "count"),
                        order_by=(),
                        limit=None,
                        required_assertions=("preserve_user_scope",),
                        input_bindings=(
                            InputBinding(
                                from_node="batches",
                                from_field="patrol_number",
                                to_field="patrol_number",
                                op="in",
                            ),
                        ),
                    ),
                    depends_on=("batches",),
                ),
            ),
            response_mode="analysis",
        )

        completed = complete_recent_window_plan(plan)
        batches, counts = completed.nodes

        self.assertEqual(
            batches.spec.dimensions,
            ("patrol_number", "create_time"),
        )
        self.assertEqual(
            batches.spec.output_projection,
            ("patrol_number", "create_time"),
        )
        self.assertNotIn("route_name", batches.spec.dimensions)
        self.assertIn(
            "complete_recent_window",
            counts.spec.required_assertions,
        )

    def test_recent_window_is_resolved_from_patrol_batches_before_disease_counts(self):
        plan = complete_recent_window_plan(_recent_plan())

        self.assertEqual([node.id for node in plan.nodes], [
            "recent_patrol_batches",
            "counts",
        ])
        batches, counts = plan.nodes
        self.assertEqual(batches.spec.subject, "patrol")
        self.assertEqual(batches.spec.view, "list")
        self.assertEqual(batches.spec.limit, 5)
        self.assertEqual(
            batches.spec.order_by,
            ({"field": "create_time", "direction": "desc"},),
        )
        self.assertIn("recent_window", batches.spec.required_assertions)
        self.assertEqual(counts.depends_on, ("recent_patrol_batches",))
        self.assertIsNone(counts.spec.limit)
        self.assertEqual(len(counts.spec.input_bindings), 1)
        binding = counts.spec.input_bindings[0]
        self.assertEqual(binding.from_node, "recent_patrol_batches")
        self.assertEqual(binding.from_field, "patrol_number")
        self.assertEqual(binding.to_field, "patrol_number")
        self.assertEqual(binding.op, "in")

    def test_missing_disease_counts_are_filled_for_every_real_batch(self):
        batches, counts = complete_recent_window_plan(_recent_plan()).nodes
        batch_observation = ExecutionObservation(
            node_id=batches.id,
            ok=True,
            engine="wren",
            sql="SELECT patrol_number, create_time FROM patrol",
            columns=("任务编号", "创建时间"),
            row_count=5,
            preview_rows=(
                {"任务编号": "P5", "创建时间": "2026-11-05"},
                {"任务编号": "P4", "创建时间": "2026-11-04"},
                {"任务编号": "P3", "创建时间": "2026-11-03"},
                {"任务编号": "P2", "创建时间": "2026-11-02"},
                {"任务编号": "P1", "创建时间": "2026-11-01"},
            ),
            result_rows=(
                {"任务编号": "P5", "创建时间": "2026-11-05"},
                {"任务编号": "P4", "创建时间": "2026-11-04"},
                {"任务编号": "P3", "创建时间": "2026-11-03"},
                {"任务编号": "P2", "创建时间": "2026-11-02"},
                {"任务编号": "P1", "创建时间": "2026-11-01"},
            ),
            rows_fetched=5,
        )
        count_observation = ExecutionObservation(
            node_id=counts.id,
            ok=True,
            engine="wren",
            sql="SELECT patrol_number, COUNT(*) FROM disease",
            columns=("任务编号", "数量"),
            row_count=3,
            preview_rows=(
                {"任务编号": "P3", "数量": "3"},
                {"任务编号": "P1", "数量": "1"},
                {"任务编号": "P2", "数量": "1"},
            ),
            result_rows=(
                {"任务编号": "P3", "数量": "3"},
                {"任务编号": "P1", "数量": "1"},
                {"任务编号": "P2", "数量": "1"},
            ),
            rows_fetched=3,
        )

        rows, filled = complete_recent_window_groups(
            counts.spec,
            count_observation,
            batches.spec,
            batch_observation,
        )

        self.assertEqual(
            [(row["任务编号"], row["数量"]) for row in rows],
            [
                ("P5", 0),
                ("P4", 0),
                ("P3", "3"),
                ("P2", "1"),
                ("P1", "1"),
            ],
        )
        self.assertEqual(filled, ["P5", "P4"])


if __name__ == "__main__":
    unittest.main()
