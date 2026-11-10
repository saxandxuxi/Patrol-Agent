from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import FAILURE_CODES
from ..models import SqlIssue


def sql_issue(
    code: str,
    *,
    path: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> SqlIssue:
    return SqlIssue(
        code=code,
        message=FAILURE_CODES.get(code, "SQL 审计失败，原因未知"),
        path=path,
        evidence=dict(evidence or {}),
    )
