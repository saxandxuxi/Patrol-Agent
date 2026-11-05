from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .contracts import QuerySpec, UserDecision
from .templates import TemplateCandidate


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    engine: str
    source: str
    template_id: str | None = None
    template_version: int | None = None
    kind: str = "mysql"


TemplateExecutor = Callable[[TemplateCandidate, dict[str, Any], QuerySpec], str]
WrenExecutor = Callable[[QuerySpec], str]


def compile_query(
    spec: QuerySpec,
    decision: UserDecision,
    candidate: TemplateCandidate | None,
    template_executor: TemplateExecutor | None,
    wren_executor: WrenExecutor | None,
) -> CompiledQuery:
    if candidate is not None:
        if not decision.token:
            raise ValueError("template compilation requires a consumed confirmation")
        confirmed_id = str(decision.choices.get(spec.id) or "")
        if confirmed_id != candidate.candidate_id:
            raise ValueError("template candidate does not match confirmed choice")
        if template_executor is None:
            raise ValueError("template executor is required for a confirmed template")
        sql = str(
            template_executor(
                candidate,
                dict(decision.overrides.get(spec.id) or {}),
                spec,
            )
            or ""
        ).strip()
        if not sql:
            raise ValueError("template executor returned empty SQL")
        return CompiledQuery(
            sql=sql,
            engine=f"template:{candidate.contract.adapter_id}",
            source=candidate.contract.source,
            template_id=candidate.contract.native_id or candidate.contract.id,
            template_version=candidate.contract.version,
        )
    if wren_executor is None:
        raise ValueError("wren executor is required when no template is selected")
    sql = str(wren_executor(spec) or "").strip()
    if not sql:
        raise ValueError("wren executor returned empty SQL")
    return CompiledQuery(sql=sql, engine="wren", source="wren")
