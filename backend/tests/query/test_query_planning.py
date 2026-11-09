import unittest
from dataclasses import replace
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import (
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecError,
    QuerySpecUnsupported,
    ValueBinding,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.input_bindings import query_plan_input_binding_issue
from app.services.plan_contract import build_step_contract


class QueryPlanningTests(unittest.TestCase):
    @patch("app.query.analysis.chat_json")
    def test_input_row_index_round_trips_from_query_spec(self, chat_json):
        chat_json.return_value = {
            "final_goal": "用第二次巡查编号统计坑槽",
            "intent_family": "data",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按巡检时间倒序取最近两次巡查编号",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [
                        {"field": "collection_time", "direction": "desc"}
                    ],
                    "limit": 2,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "query_data",
                    "question": "按第二次巡查编号统计坑槽数量",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": ["count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                    "inputs": [
                        {
                            "from_node": "q1",
                            "from_field": "patrol_number",
                            "to_field": "patrol_number",
                            "op": "=",
                            "row_index": 1,
                        }
                    ],
                },
            ],
        }

        plan = analyze_single_turn(
            "倒数第二次巡查的坑槽数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        self.assertEqual(
            plan.nodes[1].spec.input_bindings[0].row_index,
            1,
        )

    def test_dependency_rejects_incompatible_source_field(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=("collection_time",),
            order_by=({"field": "collection_time", "direction": "desc"},),
            limit=1,
            required_assertions=(),
        )
        target = QuerySpec(
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
                    from_field="collection_time",
                    to_field="patrol_number",
                    op="=",
                ),
            ),
        )
        plan = QueryPlan(
            question="最近一次巡检里坑槽有多少处？",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "input_field_incompatible")

    def test_query_data_dependency_requires_executable_input_binding(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name", "pile_segment_1000m"),
            filters=(),
            output_projection=(
                "problem_type_name",
                "pile_segment_1000m",
            ),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        target = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("problem_type_name", "pile_segment_1000m"),
            filters=(),
            output_projection=(
                "problem_type_name",
                "pile_segment_1000m",
            ),
            order_by=({"field": "count", "direction": "desc"},),
            limit=1,
            required_assertions=(),
        )
        plan = QueryPlan(
            question="按病害类型给出病害最集中的桩号段",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "missing_input_binding")
        self.assertEqual(issue.node_id, "q2")

    def test_input_equivalence_must_come_from_capability_config(self):
        fixture = CapabilityRegistry.load()
        aliases = dict(fixture.snapshot.aliases)
        aliases["field_equivalences"] = []
        registry = CapabilityRegistry(
            replace(fixture.snapshot, aliases=aliases)
        )
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("device_code",),
            filters=(),
            output_projection=("device_code",),
            order_by=(),
            limit=1,
            required_assertions=(),
        )
        target = QuerySpec(
            id="q2",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("equipment_id",),
            filters=(),
            output_projection=("equipment_id",),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="device_code",
                    to_field="equipment_id",
                    op="=",
                ),
            ),
        )
        plan = QueryPlan(
            question="设备编号追踪",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "input_field_incompatible")

    def test_registered_field_equivalence_allows_display_storage_binding(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="list",
            metrics=(),
            dimensions=("route_name",),
            filters=(),
            output_projection=("route_name",),
            order_by=(),
            limit=1,
            required_assertions=(),
        )
        target = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="list",
            metrics=(),
            dimensions=("routes_id",),
            filters=(),
            output_projection=("routes_id",),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="route_name",
                    to_field="routes_id",
                    op="=",
                ),
            ),
        )
        plan = QueryPlan(
            question="路线名称追踪",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNone(issue)

    def test_dependency_row_index_requires_ordered_upstream(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number",),
            order_by=(),
            limit=2,
            required_assertions=(),
        )
        target = QuerySpec(
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
                    row_index=0,
                ),
            ),
        )
        plan = QueryPlan(
            question="最近一次巡查的坑槽数量",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "input_row_order_missing")

    def test_single_value_binding_requires_one_row_from_multi_row_source(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("route_name", "up_or_down"),
            filters=(),
            output_projection=("route_name", "up_or_down", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=10,
            required_assertions=(),
        )
        target = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("up_or_down", "pile_segment_1000m"),
            filters=(),
            output_projection=("up_or_down", "pile_segment_1000m", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="route_name",
                    to_field="route_name",
                    op="=",
                ),
            ),
        )
        plan = QueryPlan(
            question="统计高发路线和方向组合里的常见桩号段",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(
            issue.code,
            "input_single_value_requires_row_index",
        )

    def test_paired_multi_row_bindings_do_not_become_independent_cross_product(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("route_name", "up_or_down"),
            filters=(),
            output_projection=("route_name", "up_or_down", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=10,
            required_assertions=(),
        )
        target = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("route_name", "up_or_down", "pile_segment_1000m"),
            filters=(),
            output_projection=(
                "route_name",
                "up_or_down",
                "pile_segment_1000m",
                "count",
            ),
            order_by=(),
            limit=None,
            required_assertions=(),
            input_bindings=(
                InputBinding(
                    from_node="q1",
                    from_field="route_name",
                    to_field="route_name",
                    op="in",
                ),
                InputBinding(
                    from_node="q1",
                    from_field="up_or_down",
                    to_field="up_or_down",
                    op="in",
                ),
            ),
        )
        plan = QueryPlan(
            question="统计高发路线和方向组合里的常见桩号段",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "input_composite_rows_unsupported")

    def test_dependency_rejects_field_not_produced_by_upstream(self):
        registry = CapabilityRegistry.load()
        source = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=("collection_time",),
            order_by=({"field": "collection_time", "direction": "desc"},),
            limit=1,
            required_assertions=(),
        )
        target = QuerySpec(
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
        plan = QueryPlan(
            question="最近一次巡查的坑槽数量",
            nodes=(
                PlanNode(id="q1", spec=source),
                PlanNode(id="q2", spec=target, depends_on=("q1",)),
            ),
        )

        issue = query_plan_input_binding_issue(plan, registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "input_field_not_produced")

    @patch("app.query.analysis.chat_json")
    def test_day_trend_requires_calendar_day_dimension(self, chat_json):
        invalid = {
            "final_goal": "按天统计病害趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按天统计病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["collection_time"],
                    "filters": [],
                    "output_projection": ["collection_time", "count"],
                    "order_by": [{"field": "collection_time", "direction": "asc"}],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        corrected = {
            **invalid,
            "views": [
                {
                    **invalid["views"][0],
                    "dimensions": ["day"],
                    "output_projection": ["day", "count"],
                    "order_by": [{"field": "day", "direction": "asc"}],
                }
            ],
        }
        chat_json.side_effect = [invalid, corrected]

        plan = analyze_single_turn(
            "按天统计2026年10月27日至11月4日的病害趋势",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(plan.nodes[0].spec.dimensions, ("day",))

    @patch("app.query.analysis.chat_json")
    def test_trend_granularity_field_is_not_required_on_post_calc(self, chat_json):
        chat_json.return_value = {
            "final_goal": "按周统计病害趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按周统计病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["week"],
                    "filters": [],
                    "output_projection": ["week", "count"],
                    "order_by": [{"field": "week", "direction": "asc"}],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "p1",
                    "action": "post_calc",
                    "question": "分析周趋势",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["analysis_only"],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }

        plan = analyze_single_turn(
            "2026年8月按周统计病害数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[0].spec.dimensions, ("week",))

    @patch("app.query.analysis.chat_json")
    def test_two_period_comparison_expands_all_declared_metrics(self, chat_json):
        chat_json.return_value = {
            "final_goal": "比较两个周期的数量和面积",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "oct",
                    "action": "query_data",
                    "question": "统计10月数量和总面积",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count", "total_area"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "nov",
                    "action": "query_data",
                    "question": "统计11月数量和总面积",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count", "total_area"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "compare_count",
                    "action": "post_calc",
                    "question": "比较数量变化",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["multi_metric_comparison"],
                    "depends_on": ["oct", "nov"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_scalar",
                        "operation": "percentage_change",
                        "operands": [
                            {
                                "name": "old",
                                "source": {"node": "oct", "field": "count"},
                            },
                            {
                                "name": "new",
                                "source": {"node": "nov", "field": "count"},
                            },
                        ],
                        "output": {
                            "name": "count_change_pct",
                            "label": "数量变化百分比",
                            "unit": "%",
                        },
                    },
                },
            ],
        }

        plan = analyze_single_turn(
            "长沙－芷江高速公路2026年10月和11月的病害数量及总面积分别是多少？请比较变化。",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        post_calcs = [
            node for node in plan.nodes if node.node_type == "post_calc"
        ]
        self.assertEqual(len(post_calcs), 2)
        sources = {
            operand.source["field"]
            for node in post_calcs
            for operand in node.spec.calculation.operands
            if operand.source
        }
        self.assertEqual(sources, {"count", "total_area"})

    @patch("app.query.analysis.chat_json")
    def test_list_view_with_metrics_without_dimensions_is_aggregate(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "统计单月数量和面积",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计10月数量和总面积",
                    "subject": "disease",
                    "view": "list",
                    "metrics": ["count", "total_area"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "2026年10月检出多少处病害、总面积是多少",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.view, "count")

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_invalid_total_post_calc_is_dropped_when_total_node_exists(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = {
            "final_goal": "逐月任务和总数",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "months",
                    "action": "query_data",
                    "question": "逐月任务数",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "total",
                    "action": "query_data",
                    "question": "总任务数",
                    "subject": "patrol",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "bad_total",
                    "action": "post_calc",
                    "question": "计算总数",
                    "subject": "patrol",
                    "view": "count",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["months"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_scalar",
                        "operation": "add",
                        "operands": [
                            {
                                "name": "values",
                                "source": {
                                    "node": "months",
                                    "field": "count",
                                },
                            }
                        ],
                    },
                },
            ],
        }

        plan = analyze_single_turn(
            "2026年6月到11月各月巡检任务数和总次数",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=CapabilityRegistry.load(),
            subject="patrol",
        )

        self.assertEqual(
            [node.id for node in plan.nodes],
            ["months", "total"],
        )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_invalid_total_post_calc_is_replaced_with_count_aggregate(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = {
            "final_goal": "逐月任务和总数",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "months",
                    "action": "query_data",
                    "question": "逐月任务数",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "bad_total",
                    "action": "post_calc",
                    "question": "计算总数",
                    "subject": "patrol",
                    "view": "count",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["months"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_scalar",
                        "operation": "add",
                        "operands": [
                            {
                                "name": "left",
                                "source": {
                                    "node": "months",
                                    "field": "count",
                                },
                            }
                        ],
                    },
                },
            ],
        }

        plan = analyze_single_turn(
            "2026年6月到11月各月巡检任务数和总次数",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=CapabilityRegistry.load(),
            subject="patrol",
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[-1].spec.view, "count")
        self.assertEqual(plan.nodes[-1].spec.metrics, ("count",))
        self.assertEqual(
            plan.nodes[-1].spec.filters,
            plan.nodes[0].spec.filters,
        )

    @patch("app.query.analysis.chat_json")
    def test_two_explicit_dates_require_separate_data_nodes(self, chat_json):
        invalid = {
            "final_goal": "两个日期的病害变化",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计10月23日至10月26日的病害",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["day"],
                    "filters": [
                        {
                            "field": "collection_time",
                            "op": ">=",
                            "raw_value": "2026-10-23",
                        },
                        {
                            "field": "collection_time",
                            "op": "<",
                            "raw_value": "2026-10-27",
                        },
                    ],
                    "output_projection": ["day", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        corrected = {
            "final_goal": "两个日期的病害变化",
            "intent_family": "distribution",
            "views": [
                {
                    **invalid["views"][0],
                    "id": "q1",
                    "question": "统计10月23日病害",
                    "filters": [
                        {
                            "field": "collection_time",
                            "op": ">=",
                            "raw_value": "2026-10-23",
                        },
                        {
                            "field": "collection_time",
                            "op": "<",
                            "raw_value": "2026-10-24",
                        },
                    ],
                },
                {
                    **invalid["views"][0],
                    "id": "q2",
                    "question": "统计10月26日病害",
                    "filters": [
                        {
                            "field": "collection_time",
                            "op": ">=",
                            "raw_value": "2026-10-26",
                        },
                        {
                            "field": "collection_time",
                            "op": "<",
                            "raw_value": "2026-10-27",
                        },
                    ],
                },
            ],
        }
        chat_json.side_effect = [invalid, corrected]

        plan = analyze_single_turn(
            "10月23日和10月26日这两次巡查，病害类型变化大不大？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(len(plan.nodes), 2)

    @patch("app.query.analysis.chat_json")
    def test_severity_field_cannot_fall_back_to_all_diseases(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "严重病害最多的路线",
            "intent_family": "top_groups",
            "unknown_terms": ["严重"],
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计全部病害数量",
                    "subject": "disease",
                    "view": "top_groups",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 5,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        with self.assertRaisesRegex(
            QuerySpecError,
            "damage_type",
        ):
            analyze_single_turn(
                "找出严重等级病害最多的前5条路线",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                subject="disease",
                registry=CapabilityRegistry.load(),
            )

    @patch("app.query.analysis.chat_json")
    def test_severity_evidence_accepts_authoritative_damage_type_field(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "严重病害最多的路线",
            "intent_family": "top_groups",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计严重病害数量",
                    "subject": "disease",
                    "view": "top_groups",
                    "metrics": ["count"],
                    "dimensions": ["damage_type"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 5,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "按严重等级统计病害数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(
            plan.nodes[0].spec.dimensions,
            ("damage_type",),
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_trend_data_node_gets_structural_post_calc(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "按周统计病害趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按周分组统计病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["week"],
                    "filters": [],
                    "output_projection": ["week", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "展示过去8周每周病害数量变化趋势",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[1].node_type, "post_calc")
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))
        self.assertNotIn(
            "analysis_only",
            plan.nodes[1].spec.required_assertions,
        )
        self.assertEqual(
            plan.nodes[1].spec.calculation.tool,
            "calculate_series",
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_trend_post_calc_uses_declared_series_operands(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "按月分析病害数量趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按月统计病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": ["month", "count"],
                    "order_by": [
                        {"field": "month", "direction": "asc"}
                    ],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "trend",
                    "action": "post_calc",
                    "question": "基于月度序列说明变化趋势",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["analysis_only"],
                    "depends_on": ["q1"],
                },
            ],
        }

        plan = analyze_single_turn(
            "长沙－芷江高速公路最近三个月的病害数量变化趋势如何？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        post_calc = plan.nodes[-1]
        self.assertIsNotNone(post_calc.spec.calculation)
        self.assertEqual(
            post_calc.spec.calculation.tool,
            "calculate_series",
        )
        self.assertNotIn(
            "analysis_only",
            post_calc.spec.required_assertions,
        )
        operands = {
            item.name: item for item in post_calc.spec.calculation.operands
        }
        self.assertEqual(
            operands["labels"].source,
            {"node": "q1", "field": "month"},
        )
        self.assertEqual(
            operands["values"].source,
            {"node": "q1", "field": "count"},
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_trend_intent_with_distribution_node_gets_post_calc(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "按周统计病害趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按周分组统计病害数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["week"],
                    "filters": [],
                    "output_projection": ["week", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "展示过去8周每周病害数量变化趋势",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertEqual(plan.nodes[-1].spec.view, "trend")
        self.assertEqual(plan.nodes[-1].depends_on, ("q1",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_raw_time_trend_dimension_defaults_to_registered_month_axis(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "最近三个月病害数量趋势",
            "intent_family": "trend",
            "answer_operation": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计最近三个月病害数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["collection_time"],
                    "filters": [],
                    "output_projection": ["collection_time", "count"],
                    "order_by": [
                        {"field": "collection_time", "direction": "asc"}
                    ],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "长沙－芷江高速公路最近三个月的病害数量变化趋势如何？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        data = plan.nodes[0]
        self.assertEqual(data.spec.view, "trend")
        self.assertEqual(data.spec.dimensions, ("month",))
        self.assertEqual(data.spec.output_projection, ("month", "count"))
        self.assertEqual(
            data.spec.order_by,
            ({"field": "month", "direction": "asc"},),
        )
        series = plan.nodes[-1]
        self.assertEqual(series.spec.calculation.tool, "calculate_series")
        operands = {
            item.name: item for item in series.spec.calculation.operands
        }
        self.assertEqual(
            operands["labels"].source,
            {"node": "q1", "field": "month"},
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_existing_analysis_post_calc_is_canonicalized_to_trend(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "按周统计病害趋势",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按周分组统计病害数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["week"],
                    "filters": [],
                    "output_projection": ["week", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "p1",
                    "action": "post_calc",
                    "question": "分析周趋势",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [
                        "analysis_only",
                        "preserve_user_scope",
                    ],
                    "depends_on": ["q1"],
                },
            ],
        }

        plan = analyze_single_turn(
            "展示过去8周每周病害数量变化趋势",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[1].spec.view, "trend")

    @patch("app.query.analysis.chat_json")
    def test_analysis_only_post_calc_uses_explanatory_data_shape(self, chat_json):
        chat_json.return_value = {
            "final_goal": "比较两个月热点位置变化",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计两个月各自的高发桩号段",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["month", "pile_segment_1000m"],
                    "filters": [],
                    "output_projection": [
                        "month",
                        "pile_segment_1000m",
                        "count",
                    ],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "p1",
                    "action": "post_calc",
                    "question": "比较两个月热点位置是否变化",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["analysis_only"],
                    "depends_on": ["q1"],
                },
            ],
        }

        plan = analyze_single_turn(
            "比较这两个月病害热点位置有没有变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            registry=CapabilityRegistry.load(),
        )

        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertEqual(plan.nodes[-1].spec.view, "data")

    @patch("app.query.analysis.chat_json")
    def test_segment_label_is_not_accompanied_by_fake_boundary_columns(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "列出1公里桩号分段内病害数量最高的三段",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按1公里桩号分段统计病害数量",
                    "subject": "disease",
                    "view": "top_groups",
                    "metrics": ["count"],
                    "dimensions": ["pile_segment_1000m"],
                    "filters": [],
                    "output_projection": [
                        "pile_segment_1000m",
                        "start_pile",
                        "end_pile",
                        "count",
                    ],
                    "order_by": [
                        {"field": "count", "direction": "desc"}
                    ],
                    "limit": 3,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "列出病害最多的前三个公里段",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        spec = plan.nodes[0].spec
        self.assertEqual(spec.dimensions, ("pile_segment_1000m",))
        self.assertEqual(
            spec.output_projection,
            ("pile_segment_1000m", "count"),
        )

    @patch("app.query.analysis.chat_json")
    def test_auxiliary_subject_is_allowed_when_final_subject_matches(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "统计最近5次巡查中的坑槽数量",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "batches",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [
                        {"field": "create_time", "direction": "desc"}
                    ],
                    "limit": 5,
                    "required_assertions": [
                        "preserve_user_scope",
                        "recent_window",
                    ],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "counts",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["patrol_number"],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "坑槽",
                        }
                    ],
                    "output_projection": ["patrol_number", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["batches"],
                    "inputs": [
                        {
                            "from_node": "batches",
                            "from_field": "patrol_number",
                            "to_field": "patrol_number",
                            "op": "in",
                        }
                    ],
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.subject, "patrol")
        self.assertEqual(plan.nodes[-1].spec.subject, "disease")

    @patch("app.query.analysis.chat_json")
    def test_recent_window_disease_plan_gets_real_patrol_batch_axis(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "统计最近五次巡查中的网状裂缝数量",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "counts",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["patrol_number"],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "网状裂缝",
                        }
                    ],
                    "output_projection": ["patrol_number", "count"],
                    "order_by": [],
                    "limit": 5,
                    "required_assertions": [
                        "preserve_user_scope",
                        "recent_window",
                    ],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "最近五次巡查，网裂的数量是不是在增加？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        batches, counts = plan.nodes
        self.assertEqual(batches.spec.subject, "patrol")
        self.assertEqual(batches.spec.limit, 5)
        self.assertEqual(
            batches.spec.order_by,
            ({"field": "create_time", "direction": "desc"},),
        )
        self.assertIsNone(counts.spec.limit)
        self.assertEqual(counts.depends_on, (batches.id,))
        self.assertEqual(
            counts.spec.input_bindings[0].from_field,
            "patrol_number",
        )

    @patch("app.query.analysis.chat_json")
    def test_trend_intent_repairs_missing_terminal_analysis(self, chat_json):
        data_view = {
            "id": "q1",
            "action": "query_data",
            "subject": "disease",
            "view": "distribution",
            "metrics": ["count"],
            "dimensions": ["patrol_number"],
            "filters": [
                {
                    "field": "problem_type_name",
                    "op": "=",
                    "raw_value": "坑槽",
                }
            ],
            "output_projection": ["patrol_number", "count"],
            "order_by": [],
            "limit": None,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": [],
            "inputs": [],
        }
        first = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [data_view],
        }
        second = {
            **first,
            "views": [
                data_view,
                {
                    "id": "trend",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }
        chat_json.side_effect = [first, second]

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(chat_json.call_count, 1)
        self.assertEqual(plan.nodes[-1].node_type, "post_calc")
        self.assertEqual(plan.response_mode, "analysis")

    @patch("app.query.analysis.chat_json")
    def test_trend_analysis_consumes_all_data_nodes(self, chat_json):
        batch_view = {
            "id": "batches",
            "action": "query_data",
            "subject": "patrol",
            "view": "list",
            "metrics": [],
            "dimensions": ["patrol_number"],
            "filters": [],
            "output_projection": ["patrol_number"],
            "order_by": [{"field": "create_time", "direction": "desc"}],
            "limit": 5,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": [],
            "inputs": [],
        }
        count_view = {
            "id": "counts",
            "action": "query_data",
            "subject": "disease",
            "view": "distribution",
            "metrics": ["count"],
            "dimensions": ["patrol_number"],
            "filters": [
                {
                    "field": "problem_type_name",
                    "op": "=",
                    "raw_value": "坑槽",
                }
            ],
            "output_projection": ["patrol_number", "count"],
            "order_by": [],
            "limit": None,
            "required_assertions": ["preserve_user_scope"],
            "depends_on": ["batches"],
            "inputs": [
                {
                    "from_node": "batches",
                    "from_field": "patrol_number",
                    "to_field": "patrol_number",
                    "op": "in",
                }
            ],
        }
        chat_json.return_value = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [
                batch_view,
                count_view,
                {
                    "id": "trend",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["counts"],
                    "inputs": [],
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[-1].depends_on, ("batches", "counts"))

    @patch("app.query.analysis.chat_json")
    def test_trend_series_uses_batch_axis_for_labels(self, chat_json):
        chat_json.return_value = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [
                {
                    "id": "batches",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [
                        {"field": "create_time", "direction": "desc"}
                    ],
                    "limit": 5,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "counts",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["patrol_number"],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "坑槽",
                        }
                    ],
                    "output_projection": ["patrol_number", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["batches"],
                    "inputs": [
                        {
                            "from_node": "batches",
                            "from_field": "patrol_number",
                            "to_field": "patrol_number",
                            "op": "in",
                        }
                    ],
                },
                {
                    "id": "trend",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["batches", "counts"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_series",
                        "operation": "subtract",
                        "operands": [
                            {
                                "name": "labels",
                                "source": {
                                    "node": "counts",
                                    "field": "patrol_number",
                                },
                            },
                            {
                                "name": "values",
                                "source": {
                                    "node": "counts",
                                    "field": "count",
                                },
                            },
                        ],
                        "precision": 6,
                    },
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        operands = {
            item.name: item
            for item in plan.nodes[-1].spec.calculation.operands
        }
        self.assertEqual(operands["labels"].source["node"], "batches")
        self.assertEqual(operands["values"].source["node"], "counts")

    @patch("app.query.analysis.chat_json")
    def test_recent_window_trend_is_split_into_batches_counts_and_series(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "分析最近5次巡查的坑槽数量变化",
            "intent_family": "trend",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["patrol_number", "collection_time"],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "坑槽",
                        }
                    ],
                    "output_projection": [
                        "patrol_number",
                        "collection_time",
                        "count",
                    ],
                    "order_by": [
                        {"field": "create_time", "direction": "asc"}
                    ],
                    "limit": 5,
                    "required_assertions": [
                        "preserve_user_scope",
                        "recent_window",
                    ],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["analysis_only"],
                    "depends_on": ["q1"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_series",
                        "operation": "subtract",
                        "operands": [
                            {
                                "name": "labels",
                                "source": {
                                    "node": "q1",
                                    "field": "patrol_number",
                                },
                            },
                            {
                                "name": "values",
                                "source": {
                                    "node": "q1",
                                    "field": "count",
                                },
                            },
                        ],
                        "precision": 0,
                    },
                },
            ],
        }

        plan = analyze_single_turn(
            "坑槽在最近5次巡查的数量变化",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        batches, counts, series = plan.nodes
        self.assertEqual(batches.spec.subject, "patrol")
        self.assertEqual(counts.spec.view, "distribution")
        self.assertEqual(counts.spec.dimensions, ("patrol_number",))
        self.assertEqual(counts.spec.order_by, ())
        self.assertEqual(series.spec.view, "trend")
        self.assertNotIn(
            "analysis_only",
            series.spec.required_assertions,
        )
        operands = {
            item.name: item for item in series.spec.calculation.operands
        }
        self.assertEqual(operands["labels"].source["node"], batches.id)
        self.assertEqual(operands["values"].source["node"], counts.id)

    @patch("app.query.analysis.chat_json")
    def test_limited_list_requires_order_before_execution(self, chat_json):
        chat_json.return_value = {
            "final_goal": "最近5次巡查",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "batches",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["patrol_number"],
                    "filters": [],
                    "output_projection": ["patrol_number"],
                    "order_by": [],
                    "limit": 5,
                    "required_assertions": [
                        "preserve_user_scope",
                        "recent_window",
                    ],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        with self.assertRaisesRegex(QuerySpecError, "必须声明 order_by"):
            analyze_single_turn(
                "最近5次巡查",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                subject="patrol",
            )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_uncompilable_plan_is_fed_back_before_audit(
        self,
        chat_json,
        _audit,
    ):
        invalid = {
            "final_goal": "列出巡查设备",
            "intent_family": "detail",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["not_a_real_field"],
                    "filters": [],
                    "output_projection": ["not_a_real_field"],
                    "order_by": [],
                    "limit": 500,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                },
            ],
        }
        valid = {
            **invalid,
            "views": [
                {
                    **invalid["views"][0],
                    "dimensions": ["equipment_id"],
                    "output_projection": ["equipment_id"],
                }
            ],
        }
        chat_json.side_effect = [invalid, valid]

        plan = analyze_single_turn(
            "列出所有巡查设备",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(plan.nodes[0].spec.dimensions, ("equipment_id",))

    @patch("app.query.analysis.chat_json")
    def test_order_by_registered_metric_is_valid(self, chat_json):
        chat_json.return_value = {
            "final_goal": "按数量排序路线",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计数量并排序",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [
                        {"field": "count", "direction": "desc"}
                    ],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "按数量排序路线",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
        )

        self.assertEqual(plan.nodes[0].spec.order_by[0]["field"], "count")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_completion_question_accepts_model_status_distribution(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "华容-常宁本月巡检完成情况",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["patrol_status"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "华容－常宁高速公路",
                        }
                    ],
                    "output_projection": ["patrol_status", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "subject": "patrol",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1"],
                    "inputs": [],
                    "question": "分析各巡检状态数量和完成情况",
                },
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="route_name",
            op="=",
            storage_value="route-id",
            display_value="华容－常宁高速公路",
            source="user_explicit",
            resolver="route_dictionary",
            raw_text="华容－常宁高速公路",
        )

        plan = analyze_single_turn(
            "华容－常宁高速公路在本月的巡检任务完成情况如何",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        spec = plan.nodes[0].spec
        self.assertEqual(spec.dimensions, ("patrol_status",))
        self.assertEqual(spec.metrics, ("count",))
        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    @patch("app.query.analysis.chat_json")
    def test_metric_objects_are_normalized_to_logical_metrics(self, chat_json):
        chat_json.return_value = {
            "final_goal": "任务次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": [{"field": "task_id", "agg": "count"}],
                    "dimensions": ["equipment_id"],
                    "filters": [],
                    "output_projection": ["equipment_id", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "每台设备分别完成多少次任务",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.metrics, ("count",))

    @patch("app.query.analysis.chat_json")
    def test_patrol_device_alias_uses_equipment_dimension(self, chat_json):
        chat_json.return_value = {
            "final_goal": "设备任务次数",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["device_id"],
                    "filters": [],
                    "output_projection": ["device_id", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "2026年11月每台巡检设备分别完成多少次巡检",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.dimensions, ("equipment_id",))
        self.assertEqual(plan.nodes[0].spec.output_projection, ("equipment_id", "count"))

    @patch("app.query.analysis.chat_json")
    def test_route_dependency_uses_display_name_contract(self, chat_json):
        chat_json.return_value = {
            "final_goal": "Top路线多指标",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "top",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["routes_id"],
                    "filters": [],
                    "output_projection": ["routes_id", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 5,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "stats",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "sum_area"],
                    "dimensions": ["routes_id"],
                    "filters": [],
                    "output_projection": ["routes_id", "count", "sum_area"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["top"],
                    "inputs": [
                        {
                            "from_node": "top",
                            "from_field": "routes_id",
                            "to_field": "routes_id",
                            "op": "in",
                        }
                    ],
                },
            ],
        }

        plan = analyze_single_turn(
            "病害数量最多的前5条路线分别有多少处、总面积多少平方米？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertEqual(plan.nodes[1].spec.dimensions, ("route_name",))
        binding = plan.nodes[1].spec.input_bindings[0]
        self.assertEqual(binding.from_field, "route_name")
        self.assertEqual(binding.to_field, "route_name")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_patrol_route_question_uses_structured_patrol_plan(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "11月2日巡检路线",
            "intent_family": "detail",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "patrol",
                    "view": "list",
                    "metrics": [],
                    "dimensions": ["route_name"],
                    "filters": [
                        {
                            "field": "create_time",
                            "op": ">=",
                            "raw_value": "2026-11-02",
                        }
                    ],
                    "output_projection": ["route_name"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="create_time",
            op=">=",
            storage_value="2026-11-02",
            display_value="2026-11-02",
            source="user_explicit",
            resolver="time_range",
            raw_text="2026-11-02",
        )

        plan = analyze_single_turn(
            "11月2日巡检了哪些线路？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.subject, "patrol")
        self.assertEqual(plan.nodes[0].spec.dimensions, ("route_name",))
        self.assertEqual(plan.nodes[0].spec.filters[0].field, "create_time")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_planner_order_alias_is_normalized(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "按数量降序",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["problem_type_name"],
                    "filters": [],
                    "output_projection": ["problem_type_name", "count"],
                    "order_by": [{"field": "count", "order": "desc"}],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "按病害类型统计数量并降序",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[0].spec.order_by[0]["direction"], "desc")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_identifier_resolution_keeps_model_task_number_filter(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "任务检出的病害分布",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计工单XJJL202611040003检出的病害分布",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["problem_type_name"],
                    "filters": [
                        {
                            "field": "task_number",
                            "op": "=",
                            "raw_value": "XJJL202611040003",
                        }
                    ],
                    "output_projection": ["problem_type_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }
        resolve_value.return_value = ValueBinding(
            field="task_number",
            op="=",
            storage_value="XJJL202611040003",
            display_value="XJJL202611040003",
            source="user_explicit",
            resolver="identifier_dictionary:XJJL",
            raw_text="XJJL202611040003",
        )

        plan = analyze_single_turn(
            "工单XJJL202611040003检出病害数量以及病害分布情况",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        filters = plan.nodes[0].spec.filters
        self.assertIn("task_number", {item.field for item in filters})
        task = next(item for item in filters if item.field == "task_number")
        self.assertEqual(task.resolver, "identifier_dictionary:XJJL")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_analysis_action_is_normalized_to_post_calc(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "计算比较结果",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "analysis",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }
        plan = analyze_single_turn(
            "计算比较结果",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    def test_prior_result_top_question_must_be_post_calc(self):
        contract = build_step_contract(
            "统计各路线下病害类型数量，找出每条路线数量最多的类型",
            {
                "type": "data",
                "question": "基于q1的结果，找出每条路线数量最多的主要病害类型",
            },
            known_damage_types=["泛油", "纵向裂缝"],
        )
        self.assertEqual(contract["status"], "invalid")
        self.assertIn(
            "query_data_over_prior_result",
            {item["code"] for item in contract["violations"]},
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_analysis_preserves_explicit_post_calc_spec(self, chat_json, _resolve):
        chat_json.return_value = {
            "final_goal": "找出每条路线的主要病害",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计各路线下各病害类型数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "基于q1的结果，找出每条路线数量最多的主要病害类型",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": ["route_name", "problem_type_name", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 1,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                },
            ],
        }
        plan = analyze_single_turn(
            "找出每条路线数量最多的主要病害类型",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )
        self.assertEqual(plan.nodes[1].node_type, "post_calc")
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))
        self.assertEqual(plan.nodes[1].spec.view, "top")
        self.assertEqual(plan.nodes[1].spec.metrics, ("count",))
        self.assertEqual(
            plan.nodes[1].spec.dimensions,
            ("route_name", "problem_type_name"),
        )

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_prior_group_result_top_is_normalized_to_post_calc(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "找出每条路线的主要病害",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计各路线下各病害类型数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "基于分组统计结果，找出每条路线下数量最多的病害类型",
                    "subject": "disease",
                    "view": "top",
                    "metrics": ["count"],
                    "dimensions": ["route_name", "problem_type_name"],
                    "filters": [],
                    "output_projection": ["route_name", "problem_type_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                },
            ],
        }

        plan = analyze_single_turn(
            "按路线和病害类型分组，统计各路线下每种病害类型的数量，找出每条路线数量最多的主要病害类型",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(plan.nodes[1].node_type, "post_calc")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_severity_question_does_not_rewrite_incomplete_model_plan(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "判断病害最严重的道路",
            "intent_family": "distribution",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按道路统计病害数量",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                }
            ],
        }

        plan = analyze_single_turn(
            "哪个道路的病害最严重？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(plan.nodes[0].spec.metrics, ("count",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_trend_question_accepts_structured_post_calculation(
        self,
        chat_json,
        _resolve,
    ):
        chat_json.return_value = {
            "final_goal": "判断病害数量趋势",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按月统计该路线病害数量",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": ["month", "count"],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                },
                {
                    "id": "q2",
                    "action": "post_calc",
                    "question": "判断月度数量趋势",
                    "subject": "disease",
                    "view": "trend",
                    "metrics": ["count"],
                    "dimensions": ["month"],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": [],
                    "depends_on": ["q1"],
                },
            ],
        }

        plan = analyze_single_turn(
            "长沙－芷江高速公路病害数量，最近是在增加还是在减少？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.nodes[1].node_type, "post_calc")
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_internal_dependency_placeholder_is_normalized_to_input_binding(
        self,
        chat_json,
        resolve_value,
    ):
        chat_json.return_value = {
            "final_goal": "先找主要路线，再统计该路线未识别病害",
            "intent_family": "analysis",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按路线统计未识别病害数量并取第一名",
                    "subject": "disease",
                    "view": "top_groups",
                    "metrics": ["count"],
                    "dimensions": ["route_name"],
                    "filters": [],
                    "output_projection": ["route_name", "count"],
                    "order_by": [{"field": "count", "direction": "desc"}],
                    "limit": 1,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "query_data",
                    "question": "统计该路线未识别病害数量",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "DEPENDS_ON_q1_route_name",
                        }
                    ],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": ["q1"],
                    "inputs": [],
                },
            ],
        }

        plan = analyze_single_turn(
            "AI认不出来的病害主要集中在哪条路上？有多少处？",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            subject="disease",
            registry=CapabilityRegistry.load(),
        )

        downstream = plan.nodes[1]
        self.assertEqual(downstream.depends_on, ("q1",))
        self.assertEqual(downstream.spec.filters, ())
        self.assertEqual(
            downstream.spec.input_bindings,
            (
                type(downstream.spec.input_bindings[0])(
                    from_node="q1",
                    from_field="route_name",
                    to_field="route_name",
                    op="=",
                ),
            ),
        )
        resolve_value.assert_not_called()
