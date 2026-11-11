import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.fields import FieldReachSet, PhysicalSchemaView


def province_density_payload() -> dict:
    disease_province_filter = [
        {
            "field": "route_level",
            "op": "=",
            "raw_value": "省道",
        }
    ]
    route_province_filter = [
        {
            "field": "level",
            "op": "=",
            "raw_value": "省道",
        }
    ]
    return {
        "final_goal": "统计省道病害总长度和每公里病害长度",
        "intent_family": "data",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "统计省道病害有效总长度",
                "subject": "disease",
                "view": "sum",
                "metrics": ["sum_effective_disease_length"],
                "dimensions": [],
                "filters": disease_province_filter,
                "output_projection": ["sum_effective_disease_length"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "q2",
                "action": "query_data",
                "question": "统计省道唯一路线总长度",
                "subject": "route",
                "view": "sum",
                "metrics": ["total_route_length_km"],
                "dimensions": [],
                "filters": route_province_filter,
                "output_projection": ["total_route_length_km"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "q3",
                "action": "post_calc",
                "question": "用病害总长度除以路线总长度计算病害长度密度",
                "subject": "disease",
                "view": "data",
                "metrics": ["disease_length_density"],
                "dimensions": [],
                "filters": [],
                "output_projection": ["disease_length_density"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": ["q1", "q2"],
                "inputs": [],
                "calculation": {
                    "tool": "calculate_scalar",
                    "operation": "divide",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {
                                "node": "q1",
                                "field": "sum_effective_disease_length",
                            },
                        },
                        {
                            "name": "denominator",
                            "source": {
                                "node": "q2",
                                "field": "total_route_length_km",
                            },
                        },
                    ],
                    "output": {
                        "name": "disease_length_density",
                        "label": "病害长度密度",
                        "unit": "米/km",
                    },
                    "precision": 4,
                },
            },
        ],
    }


def patrol_density_payload() -> dict:
    return {
        "final_goal": "统计巡查任务次数和有效巡查天数的比值",
        "intent_family": "data",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "统计巡查任务总次数",
                "subject": "patrol",
                "view": "count",
                "metrics": ["count"],
                "dimensions": [],
                "filters": [],
                "output_projection": ["count"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "q2",
                "action": "query_data",
                "question": "统计有巡查任务的有效天数",
                "subject": "patrol",
                "view": "count",
                "metrics": ["active_day_count"],
                "dimensions": [],
                "filters": [],
                "output_projection": ["active_day_count"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "q3",
                "action": "post_calc",
                "question": "用巡查任务次数除以有效巡查天数",
                "subject": "patrol",
                "view": "data",
                "metrics": ["patrol_density"],
                "dimensions": [],
                "filters": [],
                "output_projection": ["patrol_density"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": ["q1", "q2"],
                "inputs": [],
                "calculation": {
                    "tool": "calculate_scalar",
                    "operation": "divide",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q1", "field": "count"},
                        },
                        {
                            "name": "denominator",
                            "source": {
                                "node": "q2",
                                "field": "active_day_count",
                            },
                        },
                    ],
                    "output": {
                        "name": "patrol_density",
                        "label": "巡查任务密度",
                        "unit": "次/天",
                    },
                    "precision": 4,
                },
            },
        ],
    }


def top_route_then_details_payload() -> dict:
    return {
        "final_goal": "先找病害最多的路线，再列出这条路线的病害",
        "intent_family": "data",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "按路线统计病害数量并取第一名",
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
                "question": "列出该路线检出的全部病害",
                "subject": "disease",
                "view": "list",
                "metrics": [],
                "dimensions": [],
                "filters": [],
                "output_projection": [
                    "problem_type_name",
                    "problem_number",
                    "pile_number",
                ],
                "order_by": [],
                "limit": 500,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": ["q1"],
                "inputs": [
                    {
                        "from_node": "q1",
                        "from_field": "route_name",
                        "to_field": "route_name",
                        "op": "in",
                    }
                ],
            },
        ],
    }


def guodao_sections_payload() -> dict:
    return {
        "final_goal": "列出有病害且属于国道的路段",
        "intent_family": "data",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "筛选国道且至少存在一条有效病害的路段",
                "subject": "section",
                "view": "list",
                "metrics": [],
                "dimensions": [],
                "filters": [
                    {
                        "field": "route_level",
                        "op": "=",
                        "raw_value": "国道",
                    },
                    {
                        "field": "has_disease",
                        "op": "exists",
                        "raw_value": True,
                    },
                ],
                "output_projection": [
                    "route_name",
                    "full_name",
                    "start_num",
                    "end_num",
                ],
                "order_by": [],
                "limit": 500,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            }
        ],
    }


def latest_patrol_and_diseases_payload() -> dict:
    return {
        "final_goal": "读取最近一次巡查记录及其检出的病害",
        "intent_family": "data",
        "answer_operation": "single_result",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "按巡查时间倒序取最近一次巡查批次",
                "subject": "patrol",
                "view": "list",
                "metrics": [],
                "dimensions": ["patrol_number"],
                "filters": [],
                "output_projection": ["patrol_number", "create_time"],
                "order_by": [{"field": "create_time", "direction": "desc"}],
                "limit": 1,
                "required_assertions": [
                    "recent_window",
                    "preserve_user_scope",
                ],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "q2",
                "action": "query_data",
                "question": "列出该巡查批次检出的病害",
                "subject": "disease",
                "view": "list",
                "metrics": [],
                "dimensions": [],
                "filters": [],
                "output_projection": [
                    "problem_type_name",
                    "problem_number",
                    "pile_number",
                ],
                "order_by": [],
                "limit": 500,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": ["q1"],
                "inputs": [
                    {
                        "from_node": "q1",
                        "from_field": "patrol_number",
                        "to_field": "patrol_number",
                        "op": "in",
                    }
                ],
            },
        ],
    }


class FactSourceEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def build_plan(self, question: str, payload: dict, *, subject: str):
        with (
            patch(
                "app.query.analysis.audit_query_plan",
                return_value={"complete": True},
            ),
            patch("app.query.analysis.chat_json", return_value=payload),
            patch(
                "app.query.analysis.preflight_spec_mdl",
                return_value="SELECT 1",
            ),
        ):
            return analyze_single_turn(
                question,
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject=subject,
            )

    def test_province_disease_length_and_density_use_three_nodes(self):
        plan = self.build_plan(
            "省道上的病害总长度以及密度是多少？",
            province_density_payload(),
            subject="disease",
        )

        self.assertEqual([node.id for node in plan.nodes], ["q1", "q2", "q3"])
        self.assertEqual(
            [node.spec.subject for node in plan.nodes],
            ["disease", "route", "disease"],
        )
        calculation = plan.nodes[2].spec.calculation
        self.assertEqual(calculation.operation, "divide")
        self.assertEqual(calculation.output.unit, "米/km")

    def test_patrol_density_reuses_the_same_calculation_contract(self):
        plan = self.build_plan(
            "巡查任务密度是多少？",
            patrol_density_payload(),
            subject="patrol",
        )

        operands = plan.nodes[-1].spec.calculation.operands
        self.assertEqual(
            [item.source["field"] for item in operands],
            ["count", "active_day_count"],
        )
        self.assertEqual(plan.nodes[-1].spec.calculation.output.unit, "次/天")

    def test_top_route_then_disease_details_uses_structured_inputs(self):
        plan = self.build_plan(
            "病害数量最多的那条路是哪条？这条路具体有些什么病害",
            top_route_then_details_payload(),
            subject="disease",
        )

        binding = plan.nodes[1].spec.input_bindings[0]
        self.assertEqual(
            (binding.from_node, binding.from_field, binding.to_field),
            ("q1", "route_name", "route_name"),
        )
        self.assertEqual(plan.nodes[1].depends_on, ("q1",))

    def test_guodao_sections_with_disease_use_relation_exists(self):
        plan = self.build_plan(
            "有病害且是国道的路段有哪些",
            guodao_sections_payload(),
            subject="section",
        )

        filters = {
            item.field: item
            for item in plan.nodes[0].spec.filters
        }
        self.assertEqual(filters["has_disease"].op, "exists")
        self.assertTrue(filters["has_disease"].storage_value)
        self.assertNotIn("linked_disease_count", plan.nodes[0].spec.metrics)

    def test_latest_patrol_and_diseases_keep_patrol_then_disease_chain(self):
        plan = self.build_plan(
            "把最近一次巡查记录以及检测出来的病害调出来",
            latest_patrol_and_diseases_payload(),
            subject="patrol",
        )

        self.assertEqual(
            [node.spec.subject for node in plan.nodes],
            ["patrol", "disease"],
        )
        binding = plan.nodes[1].spec.input_bindings[0]
        self.assertEqual(binding.from_node, "q1")
        self.assertEqual(binding.to_field, "patrol_number")


if __name__ == "__main__":
    unittest.main()
