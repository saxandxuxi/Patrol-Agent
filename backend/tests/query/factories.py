from __future__ import annotations

from typing import Any

from app.query.contracts import PlanNode, QueryPlan, QuerySpec, ValueBinding


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


def length_query_plan() -> QueryPlan:
    length_filter = ValueBinding(
        field="effective_disease_length",
        op=">",
        storage_value=2,
        display_value="2 米",
        source="user_explicit",
        resolver="numeric",
        raw_text="大于 2 米",
    )
    return QueryPlan(
        question="病害长度大于 2 米的有多少处？把明细列出来",
        nodes=(
            PlanNode(
                id="summary",
                spec=count_spec(
                    id="summary",
                    filters=(length_filter,),
                ),
            ),
            PlanNode(
                id="detail",
                spec=detail_spec(
                    id="detail",
                    filters=(length_filter,),
                ),
            ),
        ),
    )
