from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ...services import prompts
from ...services.llm import chat_json, role_model
from .errors import RepairPatchError
from .json_patch import apply_repair_patches, patch_signatures
from .models import RepairIssue, RepairTicket


def _repair_messages(
    ticket: RepairTicket,
    *,
    capabilities: Any,
    assist_evidence: Any,
) -> list[dict[str, Any]]:
    issue = ticket.issue
    return prompts.messages(
        "query_spec_repair",
        question=ticket.question,
        draft=json.dumps(
            ticket.draft,
            ensure_ascii=False,
            default=str,
        ),
        issue=json.dumps(
            {
                "code": issue.code,
                "path": issue.path,
                "reason": issue.reason,
                "expected": issue.expected,
                "actual": issue.actual,
                "allowed_values": list(issue.allowed_values),
                "preserve": list(issue.preserve),
                "allowed_paths": list(issue.allowed_paths),
                "mutable_filter_fields": list(
                    issue.mutable_filter_fields
                ),
                "evidence": issue.evidence,
                "attempt": ticket.attempt,
                "max_attempts": ticket.max_attempts,
                "previous_signatures": sorted(ticket.previous_signatures),
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
    )


def request_repair_once(
    ticket: RepairTicket,
    *,
    capabilities: Any,
    assist_evidence: Any,
) -> dict[str, Any]:
    data = chat_json(
        _repair_messages(
            ticket,
            capabilities=capabilities,
            assist_evidence=assist_evidence,
        ),
        model=role_model("semantic"),
        max_tokens=4096,
    )
    if not isinstance(data, dict):
        raise RepairPatchError(
            "repair model did not return a JSON object"
        )
    patches = data.get("patches")
    if not isinstance(patches, list):
        raise RepairPatchError("repair model did not return patches")
    typed_patches = tuple(
        patch for patch in patches if isinstance(patch, dict)
    )
    try:
        return apply_repair_patches(ticket, patches)
    except ValueError as exc:
        raise RepairPatchError(str(exc), typed_patches) from exc


def request_repair(
    ticket: RepairTicket,
    *,
    capabilities: Any,
    assist_evidence: Any,
) -> dict[str, Any]:
    issue = ticket.issue
    rounds_left = max(1, ticket.max_attempts - ticket.attempt + 1)
    previous_signatures = set(ticket.previous_signatures)
    last_error: RepairPatchError | None = None
    for round_index in range(rounds_left):
        attempt = ticket.attempt + round_index
        try:
            return request_repair_once(
                replace(
                    ticket,
                    issue=issue,
                    attempt=attempt,
                    previous_signatures=frozenset(previous_signatures),
                ),
                capabilities=capabilities,
                assist_evidence=assist_evidence,
            )
        except RepairPatchError as exc:
            last_error = exc
            signatures = set(patch_signatures(list(exc.patches)))
            if signatures.intersection(previous_signatures):
                raise
            previous_signatures.update(signatures)
            if round_index + 1 >= rounds_left:
                break
            issue = replace(
                issue,
                evidence={
                    **issue.evidence,
                    "previous_repair_failure": {
                        "attempt": attempt,
                        "reason": str(exc),
                        "previous_patches": list(exc.patches),
                    },
                },
            )
    raise ValueError(
        "repair failed after bounded retry: "
        f"{last_error or 'unknown repair error'}"
    ) from last_error
