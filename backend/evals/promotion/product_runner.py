from __future__ import annotations

from typing import Any

from app.services import agent

from .models import ExamCase


def run_product_case(case: ExamCase) -> dict[str, Any]:
    payload = agent.handle_message(case.question)
    if payload.get("await_confirm") and payload.get("choose_token"):
        candidate_id = str(payload.get("recommended_id") or "")
        if not candidate_id:
            choices = payload.get("capability_choices") or []
            if choices:
                candidate_id = str(choices[0].get("id") or "")
        if candidate_id:
            payload = agent.handle_message(
                case.question,
                choose_token=str(payload.get("choose_token")),
                capability_id=candidate_id,
                slot_values={},
            )
    statuses = {
        str(section.get("status") or "accepted")
        for section in payload.get("sections") or ()
        if isinstance(section, dict)
    }
    verification_status = "accepted"
    for candidate in (
        "blocked",
        "error",
        "repairable",
        "failed",
        "unsupported",
        "ambiguous",
    ):
        if candidate in statuses:
            verification_status = candidate
            break
    return {
        "answer": str(payload.get("reply") or payload.get("answer") or ""),
        "error": payload.get("error"),
        "plan": payload.get("plan"),
        "sections": payload.get("sections") or [],
        "review_gate": payload.get("review_gate"),
        "verification": {"status": verification_status},
    }
