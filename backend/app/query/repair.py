from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

from .contracts import QuerySpec
from .shape_contracts import canonical_view


_ALLOWED_ACTIONS = {
    "patch_filter_value",
    "patch_metric",
    "patch_dimension",
    "patch_view_shape",
    "add_supporting_query",
    "request_template_confirmation",
    "retry_compile",
    "repair_presentation",
    "ask_user",
    "unsupported",
}


@dataclass(frozen=True)
class RepairPatch:
    action: str
    node_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        raw = json.dumps(
            {
                "action": self.action,
                "node_id": self.node_id,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_template_confirmation(candidate_id: str) -> RepairPatch:
    return RepairPatch(
        action="request_template_confirmation",
        node_id="",
        payload={"candidate_id": str(candidate_id)},
    )


def _add_unique(values: tuple[str, ...], item: Any) -> tuple[str, ...]:
    addition = str(item or "").strip()
    if not addition or addition in values:
        return values
    return values + (addition,)


def apply_repair(
    spec: QuerySpec,
    patch: RepairPatch,
    previous_signatures: set[str] | frozenset[str],
) -> QuerySpec:
    if patch.action not in _ALLOWED_ACTIONS:
        raise ValueError(f"unsupported repair action: {patch.action}")
    if patch.signature in previous_signatures:
        raise ValueError("duplicate repair patch")
    if patch.node_id and patch.node_id != spec.id:
        raise ValueError(f"repair node does not match QuerySpec: {patch.node_id}")
    if patch.action == "patch_filter_value":
        preserve = bool(patch.payload.get("same_semantic_value"))
        if not preserve:
            raise ValueError("scope cannot change during repair")
        return spec
    if patch.action == "patch_metric":
        metrics = _add_unique(spec.metrics, patch.payload.get("add"))
        return replace(
            spec,
            metrics=metrics,
            view=canonical_view(spec.view, metrics, spec.dimensions, "query_data"),
        )
    if patch.action == "patch_dimension":
        return replace(
            spec,
            dimensions=_add_unique(spec.dimensions, patch.payload.get("add")),
        )
    if patch.action == "patch_view_shape":
        view = str(patch.payload.get("view") or "").strip()
        if not view:
            raise ValueError("patch_view_shape requires a view")
        return replace(
            spec,
            view=canonical_view(view, spec.metrics, spec.dimensions, "query_data"),
        )
    if patch.action == "request_template_confirmation":
        return spec
    return spec
