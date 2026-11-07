from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AssistEvidence:
    rule_id: str
    source: str
    candidate_type: str
    raw_span: str
    candidate_field: str = ""
    candidate_value: Any = ""
    reason: str = ""
    trusted: bool = False
    discouraged_fields: tuple[str, ...] = ()
    requires_field: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "source": self.source,
            "candidate_type": self.candidate_type,
            "raw_span": self.raw_span,
            "candidate_field": self.candidate_field,
            "candidate_value": self.candidate_value,
            "reason": self.reason,
            "trusted": self.trusted,
            "discouraged_fields": list(self.discouraged_fields),
            "requires_field": self.requires_field,
        }
