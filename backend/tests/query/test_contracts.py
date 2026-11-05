import unittest

from app.query.contracts import QuerySpec, ValueBinding, plan_scope_hash, scope_hash
from tests.query.factories import count_plan


class QueryContractTests(unittest.TestCase):
    def test_storage_and_display_values_are_not_interchangeable(self):
        value = ValueBinding(
            field="up_or_down",
            op="=",
            storage_value="2",
            display_value="下行",
            source="user_explicit",
            resolver="enum:up_or_down",
            raw_text="下行",
        )
        self.assertEqual(value.storage_value, "2")
        self.assertEqual(value.display_value, "下行")

    def test_scope_hash_changes_with_user_scope(self):
        first = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        second = QuerySpec(
            **{
                **first.__dict__,
                "filters": (
                    ValueBinding(
                        field="problem_type_name",
                        op="=",
                        storage_value="泛油",
                        display_value="泛油",
                        source="dictionary",
                        resolver="damage_type",
                        raw_text="泛油",
                    ),
                ),
            }
        )
        self.assertNotEqual(scope_hash(first), scope_hash(second))

    def test_plan_hash_covers_every_node(self):
        plan = count_plan()
        changed = QuerySpec(**{**plan.nodes[0].spec.__dict__, "limit": 10})
        changed_plan = plan.__class__(
            question=plan.question,
            nodes=(plan.nodes[0].__class__(id=plan.nodes[0].id, spec=changed),),
        )
        self.assertNotEqual(plan_scope_hash(plan), plan_scope_hash(changed_plan))
