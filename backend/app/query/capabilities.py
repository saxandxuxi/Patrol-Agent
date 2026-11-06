from __future__ import annotations

from typing import Any

from ..services.resource_loader import load_yaml
from .spec_mdl import UnsupportedQuerySpec


def validate_capabilities(spec: Any) -> None:
    fields = (load_yaml("query_capabilities.yaml", {}) or {}).get("fields") or {}
    if not isinstance(fields, dict):
        return
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
        metadata = fields.get(f"{logical_table}.{field}")
        if not isinstance(metadata, dict):
            continue
        status = str(metadata.get("status") or "")
        if status in {"unavailable", "lookup_required"}:
            reason = str(metadata.get("reason") or "字段能力不可用")
            raise UnsupportedQuerySpec(reason)
