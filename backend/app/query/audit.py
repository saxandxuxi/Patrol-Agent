from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..services import prompts
from ..services.llm import chat_json, role_model


def audit_query_plan(
    question: str,
    plan: Any,
    *,
    assist_evidence: tuple[Any, ...] = (),
    answer_operations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    response = chat_json(
        prompts.messages(
            "query_spec_audit",
            question=question,
            plan_json=json.dumps(
                asdict(plan),
                ensure_ascii=False,
                default=str,
            ),
            assist_evidence=json.dumps(
                [
                    item.to_dict()
                    if hasattr(item, "to_dict")
                    else item
                    for item in assist_evidence
                ],
                ensure_ascii=False,
                default=str,
            ),
            answer_operations=json.dumps(
                answer_operations or {},
                ensure_ascii=False,
                default=str,
            ),
            current_date=datetime.now().strftime("%Y-%m-%d"),
        ),
        model=role_model("verify"),
        max_tokens=4096,
    )
    if not isinstance(response, dict):
        raise ValueError("规划审计没有返回 JSON 对象")
    return response
