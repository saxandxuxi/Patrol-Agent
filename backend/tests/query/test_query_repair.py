import unittest

from app.query.repair import (
    RepairPatch,
    apply_repair,
    request_template_confirmation,
)
from tests.query.factories import count_spec


class RepairTests(unittest.TestCase):
    def test_repair_cannot_change_scope_hash(self):
        spec = count_spec()
        patch = RepairPatch(
            action="patch_filter_value",
            node_id="q1",
            payload={"field": "problem_type_name", "value": "坑槽"},
        )
        with self.assertRaisesRegex(ValueError, "scope"):
            apply_repair(spec, patch, set())

    def test_same_patch_is_rejected(self):
        spec = count_spec()
        patch = RepairPatch(
            action="patch_metric",
            node_id="q1",
            payload={"add": "length"},
        )
        first = apply_repair(spec, patch, set())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            apply_repair(first, patch, {patch.signature})

    def test_template_repair_only_requests_confirmation(self):
        patch = request_template_confirmation("candidate-2")
        self.assertEqual(patch.action, "request_template_confirmation")
        self.assertEqual(patch.payload["candidate_id"], "candidate-2")
