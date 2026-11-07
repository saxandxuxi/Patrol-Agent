from __future__ import annotations

from typing import Any

from .capability_registry import CapabilityRegistry
from .spec_mdl import UnsupportedQuerySpec


def validate_capabilities(
    spec: Any,
    registry: CapabilityRegistry | None = None,
) -> None:
    registry = registry or CapabilityRegistry.load()
    logical_table = {
        "disease": "disease",
        "patrol": "patrol",
    }.get(str(spec.subject))
    if not logical_table:
        return
    referenced = {
        *[str(item).rsplit(".", 1)[-1] for item in spec.dimensions],
        *[str(item).rsplit(".", 1)[-1] for item in spec.output_projection],
        *[str(item.field).rsplit(".", 1)[-1] for item in spec.filters],
    }
    for field in sorted(referenced):
        capability = registry.snapshot.fields.get(logical_table, {}).get(field)
        if capability is None:
            continue
        status = str(capability.status or "")
        if status in {"unavailable", "lookup_required"}:
            reason = str(
                capability.unavailable_reason or "字段能力不可用"
            )
            raise UnsupportedQuerySpec(reason)
