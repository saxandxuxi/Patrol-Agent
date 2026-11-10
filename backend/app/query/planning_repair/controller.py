from __future__ import annotations

import json
from typing import Any

from ...services import prompts
from ...services.llm import chat_json, role_model
from .json_patch import apply_repair_patches
from .models import RepairTicket


def request_repair(
    ticket: RepairTicket,
    *,
    capabilities: Any,
    assist_evidence: Any,
) -> dict[str, Any]:
    data = chat_json(
        prompts.messages(
            "query_spec_repair",
            question=ticket.question,
            draft=json.dumps(ticket.draft, ensure_ascii=False, default=str),
            issue=json.dumps(
                {
                    "code": ticket.issue.code,
                    "path": ticket.issue.path,
                    "reason": ticket.issue.reason,
                    "expected": ticket.issue.expected,
                    "actual": ticket.issue.actual,
                    "allowed_paths": list(ticket.issue.allowed_paths),
                    "mutable_filter_fields": list(
                        ticket.issue.mutable_filter_fields
                    ),
                    "evidence": ticket.issue.evidence,
                },
                ensure_ascii=False,
                default=str,
            ),
            capabilities=json.dumps(
                capabilities,
                ensure_ascii=False,
                default=str,
            ),
            assist_evidence=json.dumps(
                assist_evidence,
                ensure_ascii=False,
                default=str,
            ),
        ),
        model=role_model("semantic"),
        max_tokens=4096,
    )
    if not isinstance(data, dict):
        raise ValueError("repair model did not return a JSON object")
    patches = data.get("patches")
    if not isinstance(patches, list):
        raise ValueError("repair model did not return patches")
    return apply_repair_patches(ticket, patches)
