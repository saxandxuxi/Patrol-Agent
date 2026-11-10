from __future__ import annotations

from .contracts import QuerySpec
from .mdl_sql import (
    MdlSqlPipelineError,
    compile_queryspec_to_mdl_sql,
    failure_message,
)


class UnsupportedQuerySpec(ValueError):
    pass


def compile_spec_mdl(spec: QuerySpec) -> str | None:
    try:
        return compile_queryspec_to_mdl_sql(spec)
    except MdlSqlPipelineError as exc:
        raise UnsupportedQuerySpec(
            failure_message(exc.failure)
        ) from exc
