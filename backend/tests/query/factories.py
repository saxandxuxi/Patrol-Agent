from __future__ import annotations

from typing import Any

from app.query.contracts import PlanNode, QueryPlan, QuerySpec


def count_spec(**overrides: Any) -> QuerySpec:
    values = {
        "id": "q1",
        "kind": "mysql",
        "subject": "disease",
        "view": "count",
        "metrics": ("count",),
        "dimensions": (),
        "filters": (),
        "output_projection": (),
        "order_by": (),
        "limit": None,
        "required_assertions": ("preserve_user_scope",),
    }
    values.update(overrides)
    return QuerySpec(**values)


def detail_spec(**overrides: Any) -> QuerySpec:
    values = {
        "id": "q1",
        "kind": "mysql",
        "subject": "disease",
        "view": "detail",
        "metrics": (),
        "dimensions": (),
        "filters": (),
        "output_projection": ("id",),
        "order_by": (),
        "limit": 500,
        "required_assertions": ("preserve_user_scope",),
    }
    values.update(overrides)
    return QuerySpec(**values)


def count_plan() -> QueryPlan:
    return QueryPlan(
        question="泛油有多少处？",
        nodes=(PlanNode(id="q1", spec=count_spec()),),
    )
