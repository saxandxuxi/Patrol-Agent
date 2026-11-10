from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def observation_group_labels(
    filters: Iterable[Any],
    field_labels: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep user-facing dimension conditions available to downstream calculations."""
    labels: dict[str, Any] = {}
    for item in filters:
        field = str(getattr(item, "field", "")).rsplit(".", 1)[-1]
        resolver = str(getattr(item, "resolver", "") or "")
        source = str(getattr(item, "source", "") or "")
        if resolver.startswith("route_"):
            label = "路线名称"
        elif resolver.startswith("damage_type_dictionary"):
            label = "病害类型"
        elif field == "up_or_down":
            label = "上下行方向"
        elif source == "previous_result":
            label = str(field_labels.get(field) or field)
        else:
            continue
        value = getattr(item, "display_value", "")
        if value not in (None, ""):
            labels[label] = value
    return labels
