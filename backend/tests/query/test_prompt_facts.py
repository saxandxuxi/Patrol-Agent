import json
import unittest

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import PUBLIC_QUERY_VIEWS
from app.query.prompt_facts import build_prompt_facts


class PromptFactsTests(unittest.TestCase):
    def test_public_views_exclude_internal_top_alias(self):
        self.assertNotIn("top", PUBLIC_QUERY_VIEWS)
        self.assertIn("top_groups", PUBLIC_QUERY_VIEWS)

    def test_prompt_facts_render_the_same_supported_views(self):
        facts = build_prompt_facts(
            CapabilityRegistry.load(),
            {"subject": "disease"},
        )
        rendered = facts.as_prompt_values()

        self.assertEqual(
            json.loads(rendered["supported_views"]),
            list(PUBLIC_QUERY_VIEWS),
        )
        self.assertNotIn("top", json.loads(rendered["supported_views"]))


if __name__ == "__main__":
    unittest.main()
