from __future__ import annotations

from .contracts import QuerySpec
from .mdl_sql import (
    MdlSqlPipelineError,
    compile_queryspec_to_mdl_sql,
    failure_message,
    preflight_mdl_sql,
)


class UnsupportedQuerySpec(ValueError):
    def __init__(self, message: str, failure=None):
        self.failure = failure
        super().__init__(message)


def compile_spec_mdl(spec: QuerySpec) -> str | None:
    try:
        return compile_queryspec_to_mdl_sql(spec)
    except MdlSqlPipelineError as exc:
        raise UnsupportedQuerySpec(
            failure_message(exc.failure),
            failure=exc.failure,
        ) from exc


def preflight_spec_mdl(spec: QuerySpec) -> str | None:
    try:
        result = preflight_mdl_sql(spec)
    except MdlSqlPipelineError as exc:
        raise UnsupportedQuerySpec(
            failure_message(exc.failure),
            failure=exc.failure,
        ) from exc
    if result is None:
        return None
    mdl_sql = str(result.candidate.mdl_sql or "").strip()
    return mdl_sql or None
