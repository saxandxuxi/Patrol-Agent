from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contracts import PUBLIC_QUERY_VIEWS

if TYPE_CHECKING:
    from .capability_registry import CapabilityRegistry


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


@dataclass(frozen=True)
class PromptFacts:
    query_semantics: Mapping[str, Any]
    answer_operations: Mapping[str, Any]
    supported_views: tuple[str, ...]
    capabilities: Mapping[str, Any]

    def as_prompt_values(self) -> dict[str, str]:
        return {
            "query_semantics": _json(self.query_semantics),
            "answer_operations": _json(self.answer_operations),
            "supported_views": _json(self.supported_views),
            "capabilities": _json(self.capabilities),
        }


def build_prompt_facts(
    registry: CapabilityRegistry | None,
    capabilities: Mapping[str, Any],
    *,
    query_semantics: Mapping[str, Any] | None = None,
) -> PromptFacts:
    semantics = (
        dict(query_semantics)
        if query_semantics is not None
        else (
            registry.snapshot.query_shapes.get("planner_semantics", {})
            if registry is not None
            else {}
        )
    )
    operations = (
        semantics.get("answer_operations")
        if isinstance(semantics, Mapping)
        else {}
    )
    return PromptFacts(
        query_semantics=semantics,
        answer_operations=operations or {},
        supported_views=PUBLIC_QUERY_VIEWS,
        capabilities=capabilities,
    )
