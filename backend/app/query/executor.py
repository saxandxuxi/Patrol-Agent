from __future__ import annotations

import hashlib
from typing import Any, Callable

from .compiler import CompiledQuery
from .contracts import ExecutionObservation


Executor = Callable[[str, str], dict[str, Any]]


def _fingerprint(sql: str) -> str:
    return hashlib.sha256(str(sql).encode("utf-8")).hexdigest()


def _first_number(payload: dict[str, Any]) -> int | None:
    rows = list(payload.get("rows") or [])
    if not rows or not isinstance(rows[0], dict):
        return None
    for value in rows[0].values():
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def execute_compiled(
    compiled: CompiledQuery,
    executor: Executor,
    display_limit: int = 500,
    count_sql: str | None = None,
) -> ExecutionObservation:
    try:
        payload = executor(compiled.kind, compiled.sql)
    except Exception as exc:  # noqa: BLE001
        return ExecutionObservation(
            node_id="",
            ok=False,
            engine=compiled.engine,
            sql=compiled.sql,
            runtime_error=f"{type(exc).__name__}: {exc}",
            query_fingerprint=_fingerprint(compiled.sql),
        )
    rows = tuple(row for row in payload.get("rows") or [] if isinstance(row, dict))
    columns = tuple(str(item) for item in payload.get("columns") or [])
    total_count: int | None = None
    count_error: str | None = None
    if count_sql:
        try:
            total_count = _first_number(executor(compiled.kind, count_sql))
        except Exception as exc:  # noqa: BLE001
            count_error = f"{type(exc).__name__}: {exc}"
    rows_fetched = len(rows)
    truncated = (
        total_count > rows_fetched
        if total_count is not None
        else rows_fetched >= int(display_limit)
    )
    return ExecutionObservation(
        node_id="",
        ok=True,
        engine=compiled.engine,
        sql=compiled.sql,
        columns=columns,
        row_count=rows_fetched,
        preview_rows=rows[:10],
        rows_fetched=rows_fetched,
        truncated=truncated,
        total_count=total_count,
        coverage={"count_error": count_error} if count_error else {},
        query_fingerprint=_fingerprint(compiled.sql),
    )
