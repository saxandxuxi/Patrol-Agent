from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts import FailureEvidence


@dataclass(frozen=True)
class CapabilityIssue:
    code: str
    original_value: str
    reason: str
    allowed_values: tuple[str, ...] = ()
    suggested_repair: str = ""
    source: str = "capability_registry"
    node_id: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_feedback(self) -> dict[str, Any]:
        return {
            "failure_code": self.code,
            "node_id": self.node_id,
            "original_value": self.original_value,
            "reason": self.reason,
            "allowed_values": list(self.allowed_values),
            "suggested_repair": self.suggested_repair,
            "source": self.source,
            "evidence": self.evidence,
        }


def to_failure_evidence(issue: CapabilityIssue) -> FailureEvidence:
    return FailureEvidence(
        accepted=False,
        stage="capability_contract",
        failure_code=issue.code,
        node_id=issue.node_id,
        reason=issue.reason,
        expected=", ".join(issue.allowed_values),
        actual=issue.original_value,
        evidence=issue.to_feedback(),
        allowed_actions=("rewrite_query_spec",),
        max_repair_rounds=2,
    )
