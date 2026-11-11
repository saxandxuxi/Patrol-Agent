from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from ..services import prompts
from ..services.llm import chat_json, role_model
from .capability_registry import CapabilityRegistry
from .prompt_facts import build_prompt_facts


def audit_query_plan(
    question: str,
    plan: Any,
    *,
    capabilities: Mapping[str, Any] | None = None,
    registry: CapabilityRegistry | None = None,
) -> dict[str, Any]:
    prompt_facts = build_prompt_facts(
        registry,
        capabilities or {},
    ).as_prompt_values()
    response = chat_json(
        prompts.messages(
            "query_spec_audit",
            question=question,
            plan_json=json.dumps(
                asdict(plan),
                ensure_ascii=False,
                default=str,
            ),
            query_semantics=prompt_facts["query_semantics"],
            answer_operations=prompt_facts["answer_operations"],
            supported_views=prompt_facts["supported_views"],
            capabilities=prompt_facts["capabilities"],
        ),
        model=role_model("verify"),
        max_tokens=4096,
    )
    if not isinstance(response, dict):
        raise ValueError("规划审计没有返回 JSON 对象")
    return response
