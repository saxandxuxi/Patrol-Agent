import unittest

from app.query.planning_repair import (
    RepairIssue,
    RepairTicket,
    apply_repair_patches,
    repair_issue_from_capability,
    repair_issue_from_error,
    scope_signature,
)
from app.query.capability_registry import CapabilityRegistry
from app.query.capability_registry.failures import CapabilityIssue
from app.query.contracts import QuerySpecError


def _draft() -> dict:
    return {
        "final_goal": "统计坑槽数量",
        "views": [
            {
                "id": "q1",
                "question": "统计坑槽数量",
                "subject": "section",
                "view": "count",
                "metrics": ["count"],
                "dimensions": [],
                "depends_on": [],
                "filters": [
                    {
                        "field": "problem_type_name",
                        "op": "=",
                        "raw_value": "坑槽",
                    }
                ],
            }
        ],
    }


def _ticket(
    *,
    issue: RepairIssue,
    draft: dict | None = None,
) -> RepairTicket:
    return RepairTicket(
        question="统计坑槽数量",
        draft=draft or _draft(),
        issue=issue,
    )


class PlanningRepairTests(unittest.TestCase):
    def test_invalid_dependency_issue_only_allows_structural_binding_repair(self):
        issue = repair_issue_from_error(
            QuerySpecError(
                "invalid_dependency_value: q1_top_route.route_name"
            ),
            _draft(),
        )

        self.assertEqual(issue.code, "invalid_dependency_value")
        self.assertIn("/views/*/filters", issue.allowed_paths)
        self.assertIn("/views/*/depends_on", issue.allowed_paths)
        self.assertIn("/views/*/inputs", issue.allowed_paths)

    def test_soft_delete_filter_is_repairable_without_unlocking_user_scope(self):
        draft = _draft()
        draft["views"][0].update(
            {
                "subject": "work_order",
                "filters": [
                    {
                        "field": "del_flag",
                        "op": "=",
                        "raw_value": "0",
                    },
                    {
                        "field": "route_name",
                        "op": "=",
                        "raw_value": "长沙－芷江高速公路",
                    },
                ],
            }
        )
        issue = repair_issue_from_error(
            RuntimeError(
                "过滤值 del_flag 来源为 model_inference，"
                "但没有经过字典、业务规则或受控解析"
            ),
            draft,
            CapabilityRegistry.load(),
        )

        self.assertEqual(issue.mutable_filter_fields, ("del_flag",))
        repaired = apply_repair_patches(
            _ticket(issue=issue, draft=draft),
            [{"op": "remove", "path": "/views/0/filters/0"}],
        )

        self.assertEqual(
            repaired["views"][0]["filters"],
            [
                {
                    "field": "route_name",
                    "op": "=",
                    "raw_value": "长沙－芷江高速公路",
                }
            ],
        )
        with self.assertRaisesRegex(ValueError, "changed the user scope"):
            apply_repair_patches(
                _ticket(issue=issue, draft=draft),
                [{"op": "remove", "path": "/views/0/filters/1"}],
            )

    def test_permitted_subject_patch_changes_only_the_subject(self):
        ticket = _ticket(
            issue=RepairIssue(
                code="subject_conflict",
                path="/views/0/subject",
                reason="最终主体不一致",
                expected="disease",
                actual="section",
                allowed_paths=("/views/0/subject",),
            )
        )
        before_scope = scope_signature(ticket.draft)

        repaired = apply_repair_patches(
            ticket,
            [
                {
                    "op": "set",
                    "path": "/views/0/subject",
                    "value": "disease",
                }
            ],
        )

        self.assertEqual(repaired["views"][0]["subject"], "disease")
        self.assertEqual(scope_signature(repaired), before_scope)
        self.assertEqual(ticket.draft["views"][0]["subject"], "section")

    def test_filter_change_is_rejected_when_not_in_allowed_paths(self):
        ticket = _ticket(
            issue=RepairIssue(
                code="invalid_dimension",
                path="/views/0/dimensions",
                reason="维度不存在",
                allowed_paths=("/views/0/dimensions",),
            )
        )

        with self.assertRaisesRegex(ValueError, "path is not allowed"):
            apply_repair_patches(
                ticket,
                [
                    {
                        "op": "set",
                        "path": "/views/0/filters/0/raw_value",
                        "value": "裂缝",
                    }
                ],
            )

    def test_nested_patch_is_allowed_under_an_allowed_repair_path(self):
        draft = _draft()
        draft["views"][0]["order_by"] = [
            {"field": "invented_dimension", "direction": "desc"}
        ]
        ticket = _ticket(
            draft=draft,
            issue=RepairIssue(
                code="invalid_order_field",
                path="/views/0/order_by",
                reason="排序字段不存在",
                allowed_paths=("/views/*/order_by",),
            ),
        )

        repaired = apply_repair_patches(
            ticket,
            [
                {
                    "op": "set",
                    "path": "/views/0/order_by/0/field",
                    "value": "count",
                }
            ],
        )

        self.assertEqual(
            repaired["views"][0]["order_by"][0]["field"],
            "count",
        )

    def test_date_filter_repair_cannot_change_route_scope(self):
        draft = _draft()
        draft["views"][0]["filters"].insert(
            0,
            {
                "field": "route_name",
                "op": "=",
                "raw_value": "长沙－芷江高速公路",
            },
        )
        draft["views"][0]["filters"].append(
            {
                "field": "collection_time",
                "op": ">=",
                "raw_value": "2026-11-02",
            }
        )
        ticket = _ticket(
            draft=draft,
            issue=RepairIssue(
                code="missing_date_comparison_nodes",
                path="/views",
                reason="需要拆分两个日期",
                allowed_paths=(
                    "/views",
                    "/views/*",
                    "/views/*/filters",
                ),
                mutable_filter_fields=("collection_time",),
            ),
        )

        repaired = apply_repair_patches(
            ticket,
            [
                {
                    "op": "set",
                    "path": "/views/0/filters/2/raw_value",
                    "value": "2026-11-03",
                }
            ],
        )

        self.assertEqual(
            repaired["views"][0]["filters"][2]["raw_value"],
            "2026-11-03",
        )
        with self.assertRaisesRegex(ValueError, "changed the user scope"):
            apply_repair_patches(
                ticket,
                [
                    {
                        "op": "set",
                        "path": "/views/0/filters/0/raw_value",
                        "value": "许昌-广州",
                    }
                ],
            )

    def test_new_batch_view_may_reuse_an_existing_scope_filter(self):
        draft = _draft()
        draft["views"][0]["filters"].insert(
            0,
            {
                "field": "route_name",
                "op": "=",
                "raw_value": "长沙－芷江高速公路",
            },
        )
        ticket = _ticket(
            draft=draft,
            issue=RepairIssue(
                code="missing_recent_window_dependency",
                path="/views",
                reason="缺少真实巡检批次依赖",
                allowed_paths=(
                    "/views",
                    "/views/*",
                    "/views/*/filters",
                ),
            ),
        )

        repaired = apply_repair_patches(
            ticket,
            [
                {
                    "op": "add",
                    "path": "/views/0",
                    "value": {
                        "id": "batches",
                        "subject": "patrol",
                        "filters": [
                            {
                                "field": "route_name",
                                "op": "=",
                                "raw_value": "长沙－芷江高速公路",
                            }
                        ],
                    },
                }
            ],
        )

        self.assertEqual(repaired["views"][0]["id"], "batches")
        self.assertEqual(
            repaired["views"][0]["filters"][0]["raw_value"],
            "长沙－芷江高速公路",
        )

    def test_filter_scope_cannot_move_between_existing_nodes(self):
        draft = _draft()
        draft["views"][0]["filters"] = [
            {
                "field": "route_name",
                "op": "=",
                "raw_value": "长沙－芷江高速公路",
            }
        ]
        draft["views"].append(
            {
                "id": "q2",
                "question": "统计坑槽长度",
                "subject": "disease",
                "view": "metrics",
                "metrics": ["sum_disease_length"],
                "dimensions": [],
                "filters": [
                    {
                        "field": "problem_type_name",
                        "op": "=",
                        "raw_value": "坑槽",
                    },
                    {
                        "field": "up_or_down",
                        "op": "=",
                        "raw_value": "下行",
                    },
                ],
            }
        )
        ticket = _ticket(
            draft=draft,
            issue=RepairIssue(
                code="missing_required_field",
                path="/views",
                reason="需要补充范围条件",
                allowed_paths=("/views/*/filters/*",),
            ),
        )

        with self.assertRaisesRegex(ValueError, "changed the user scope"):
            apply_repair_patches(
                ticket,
                [
                    {
                        "op": "add",
                        "path": "/views/0/filters/-",
                        "value": {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "坑槽",
                        },
                    },
                    {
                        "op": "remove",
                        "path": "/views/1/filters/0",
                    },
                ],
            )

    def test_scope_signature_ignores_planning_text_but_preserves_filters(self):
        draft = _draft()
        same_scope = _draft()
        same_scope["views"][0]["question"] = "查一下坑槽有多少"
        other_scope = _draft()
        other_scope["views"][0]["filters"][0]["raw_value"] = "裂缝"

        self.assertEqual(scope_signature(draft), scope_signature(same_scope))
        self.assertNotEqual(scope_signature(draft), scope_signature(other_scope))

    def test_duplicate_patch_signature_is_rejected(self):
        ticket = _ticket(
            issue=RepairIssue(
                code="invalid_dimension",
                path="/views/0/dimensions",
                reason="维度不存在",
                allowed_paths=("/views/0/dimensions",),
            )
        )
        patch = {
            "op": "set",
            "path": "/views/0/dimensions",
            "value": ["route_name"],
        }

        with self.assertRaisesRegex(ValueError, "duplicate repair patch"):
            apply_repair_patches(ticket, [patch, dict(patch)])

    def test_capability_failure_builds_a_traceable_repair_issue(self):
        draft = _draft()
        failure = CapabilityIssue(
            code="unknown_metric",
            original_value="fake_metric",
            reason="未注册指标 fake_metric",
            allowed_values=("count", "sum_area"),
            suggested_repair="只使用允许值",
            node_id="q1",
        )

        issue = repair_issue_from_capability(failure, draft)

        self.assertEqual(issue.code, "unknown_metric")
        self.assertEqual(issue.path, "/views/0/metrics")
        self.assertEqual(issue.actual, "fake_metric")
        self.assertEqual(issue.allowed_values, ("count", "sum_area"))
        self.assertEqual(
            issue.evidence["scope_signature"],
            scope_signature(draft),
        )
        self.assertIn("original_question", issue.preserve)

    def test_entity_comparison_repair_can_only_change_evidence_dependency_shape(
        self,
    ):
        draft = _draft()
        draft["views"].extend(
            [
                {
                    "id": "q2",
                    "question": "统计当前批次病害",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "filters": [
                        {
                            "field": "collection_time",
                            "op": ">=",
                            "raw_value": "2026-11-02",
                        }
                    ],
                },
                {
                    "id": "q3",
                    "question": "比较两批病害集合",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "depends_on": ["q1", "q2"],
                    "comparison_depends_on": ["q1", "q2"],
                    "required_assertions": ["dataset_comparison"],
                },
            ]
        )
        failure = CapabilityIssue(
            code="dataset_comparison_requires_entity_details",
            original_value="count",
            reason="实体集合比较不能用 COUNT 聚合结果",
            allowed_values=("list", "detail"),
            node_id="q3",
            evidence={
                "dependency": "q2",
                "view": "count",
            },
        )

        issue = repair_issue_from_capability(failure, draft)

        self.assertIn("/views/1/view", issue.allowed_paths)
        self.assertIn("/views/1/metrics", issue.allowed_paths)
        self.assertIn("/views/1/dimensions", issue.allowed_paths)
        self.assertIn("/views/1/output_projection", issue.allowed_paths)
        self.assertNotIn("/views/1/filters", issue.allowed_paths)

        repaired = apply_repair_patches(
            _ticket(issue=issue, draft=draft),
            [
                {
                    "op": "set",
                    "path": "/views/1/view",
                    "value": "list",
                },
                {
                    "op": "set",
                    "path": "/views/1/metrics",
                    "value": [],
                },
                {
                    "op": "set",
                    "path": "/views/1/output_projection",
                    "value": ["problem_number", "pile_number"],
                },
            ],
        )

        self.assertEqual(repaired["views"][1]["view"], "list")
        self.assertEqual(
            repaired["views"][1]["filters"],
            draft["views"][1]["filters"],
        )

    def test_dataset_identity_repair_can_add_upstream_output_only(self):
        draft = _draft()
        draft["views"][0].update(
            {
                "subject": "disease",
                "view": "list",
                "metrics": [],
                "output_projection": ["pile_number", "area"],
            }
        )
        draft["views"].append(
            {
                "id": "compare",
                "question": "比较两次巡查的病害集合",
                "subject": "disease",
                "view": "data",
                "metrics": [],
                "dimensions": [],
                "filters": [],
                "depends_on": ["q1"],
                "comparison_depends_on": ["q1"],
                "required_assertions": ["dataset_comparison"],
            }
        )
        failure = CapabilityIssue(
            code="dataset_comparison_missing_identity",
            original_value="q1",
            reason="节点 q1 没有输出实体身份字段",
            allowed_values=("problem_number",),
            node_id="compare",
            evidence={
                "dependency": "q1",
                "produced_fields": ["area", "pile_number"],
            },
        )

        issue = repair_issue_from_capability(failure, draft)

        self.assertIn("/views/0/output_projection", issue.allowed_paths)
        self.assertNotIn("/views/0/filters", issue.allowed_paths)
        repaired = apply_repair_patches(
            _ticket(issue=issue, draft=draft),
            [
                {
                    "op": "set",
                    "path": "/views/0/output_projection",
                    "value": ["problem_number", "pile_number", "area"],
                }
            ],
        )
        self.assertEqual(
            repaired["views"][0]["filters"],
            draft["views"][0]["filters"],
        )
        self.assertIn(
            "problem_number",
            repaired["views"][0]["output_projection"],
        )

    def test_self_dependency_patch_is_rejected(self):
        ticket = _ticket(
            issue=RepairIssue(
                code="missing_dependency",
                path="/views/0/depends_on",
                reason="补齐依赖",
                allowed_paths=("/views/*/depends_on",),
            )
        )

        with self.assertRaisesRegex(ValueError, "self dependency"):
            apply_repair_patches(
                ticket,
                [
                    {
                        "op": "set",
                        "path": "/views/0/depends_on",
                        "value": ["q1"],
                    }
                ],
            )

    def test_patch_that_does_not_change_the_draft_is_rejected(self):
        ticket = _ticket(
            issue=RepairIssue(
                code="subject_conflict",
                path="/views/0/subject",
                reason="修正主体",
                allowed_paths=("/views/0/subject",),
            )
        )

        with self.assertRaisesRegex(ValueError, "did not change"):
            apply_repair_patches(
                ticket,
                [
                    {
                        "op": "set",
                        "path": "/views/0/subject",
                        "value": "section",
                    }
                ],
            )

    def test_cyclic_dependency_patch_is_rejected(self):
        draft = _draft()
        draft["views"][0]["depends_on"] = ["q2"]
        draft["views"].append(
            {
                "id": "q2",
                "question": "统计裂缝数量",
                "subject": "disease",
                "view": "count",
                "metrics": ["count"],
                "dimensions": [],
                "depends_on": [],
                "filters": [],
            }
        )
        ticket = _ticket(
            draft=draft,
            issue=RepairIssue(
                code="missing_dependency",
                path="/views/1/depends_on",
                reason="补齐依赖",
                allowed_paths=("/views/*/depends_on",),
            ),
        )

        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            apply_repair_patches(
                ticket,
                [
                    {
                        "op": "set",
                        "path": "/views/1/depends_on",
                        "value": ["q1"],
                    }
                ],
            )

    def test_json_pointer_escaping_is_applied(self):
        draft = {"a/b": {"m~n": 1}}
        ticket = _ticket(
            draft=draft,
            issue=RepairIssue(
                code="invalid_field",
                path="/a~1b/m~0n",
                reason="字段错误",
                allowed_paths=("/a~1b/m~0n",),
            ),
        )

        repaired = apply_repair_patches(
            ticket,
            [
                {
                    "op": "set",
                    "path": "/a~1b/m~0n",
                    "value": 2,
                }
            ],
        )

        self.assertEqual(repaired["a/b"]["m~n"], 2)
        self.assertEqual(draft["a/b"]["m~n"], 1)


if __name__ == "__main__":
    unittest.main()
