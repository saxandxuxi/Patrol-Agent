import unittest

from app.query.planning_repair import (
    RepairIssue,
    RepairTicket,
    apply_repair_patches,
    scope_signature,
)


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
