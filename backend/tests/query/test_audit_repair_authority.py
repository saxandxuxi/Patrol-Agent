import unittest
from unittest.mock import patch

from app.query.analysis import repair_audit_issues
from app.query.capability_registry import CapabilityRegistry
from app.query.planning_repair import (
    RepairTicket,
    apply_repair_patches,
    repair_issue_from_audit,
)


def _draft() -> dict:
    return {
        "final_goal": "统计路线范围内的坑槽数量",
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "统计长沙－芷江高速公路本月的坑槽数量",
                "subject": "disease",
                "view": "count",
                "metrics": ["count"],
                "dimensions": [],
                "filters": [
                    {
                        "field": "route_name",
                        "op": "=",
                        "raw_value": "长沙－芷江高速公路",
                    },
                    {
                        "field": "collection_time",
                        "op": ">=",
                        "raw_value": "2026-11-01",
                    },
                ],
                "output_projection": ["count"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            }
        ],
    }


class AuditRepairAuthorityTests(unittest.TestCase):
    @patch("app.query.analysis.request_repair_once")
    def test_audit_set_is_not_applied_without_a_repair_patch(
        self,
        request_repair_once,
    ):
        draft = _draft()
        request_repair_once.return_value = draft

        repaired = repair_audit_issues(
            draft,
            [
                {
                    "code": "lost_filter",
                    "node_id": "q1",
                    "set": {"filters": [{"field": "route_name", "op": "="}]},
                }
            ],
            registry=CapabilityRegistry.load(),
        )

        self.assertEqual(repaired, draft)
        self.assertEqual(request_repair_once.call_count, 1)

    def test_lost_filter_repair_cannot_remove_existing_user_scope(self):
        draft = _draft()
        issue = repair_issue_from_audit(
            {"code": "lost_filter", "node_id": "q1"},
            draft,
            CapabilityRegistry.load(),
        )

        with self.assertRaisesRegex(ValueError, "repair changed the user scope"):
            apply_repair_patches(
                RepairTicket(
                    question="统计路线范围内的坑槽数量",
                    draft=draft,
                    issue=issue,
                ),
                [{"op": "remove", "path": "/views/0/filters/0"}],
            )

    def test_audit_suggestion_cannot_unlock_subject_or_filter_scope(self):
        issue = repair_issue_from_audit(
            {
                "code": "invalid_view_shape",
                "node_id": "q1",
                "set": {
                    "subject": "patrol",
                    "filters": [{"field": "route_name", "op": "="}],
                    "view": "distribution",
                },
            },
            _draft(),
            CapabilityRegistry.load(),
        )

        self.assertNotIn("/views/*/subject", issue.allowed_paths)
        self.assertNotIn("/views/*/filters", issue.allowed_paths)
        self.assertIn("/views/*/view", issue.allowed_paths)

    def test_subject_conflict_ticket_only_unlocks_subject(self):
        issue = repair_issue_from_audit(
            {"code": "subject_conflict", "node_id": "q1"},
            _draft(),
            CapabilityRegistry.load(),
        )

        self.assertIn("/views/0/subject", issue.allowed_paths)
        self.assertNotIn("/views/0/filters", issue.allowed_paths)


if __name__ == "__main__":
    unittest.main()
