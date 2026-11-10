from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RepairIssue:
    code: str
    path: str
    reason: str
    expected: str = ""
    actual: str = ""
    allowed_paths: tuple[str, ...] = ()
    mutable_filter_fields: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairTicket:
    question: str
    draft: dict[str, Any]
    issue: RepairIssue
    attempt: int = 1
    max_attempts: int = 2
    previous_signatures: frozenset[str] = frozenset()
