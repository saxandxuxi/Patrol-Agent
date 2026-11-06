from __future__ import annotations

from collections import defaultdict

from .capability_registry.models import MetricCapability


def compile_registered_metric(
    subject: str,
    metric: MetricCapability,
    aliases: dict[str, str] | None = None,
) -> str:
    alias_map: defaultdict[str, str] = defaultdict(str)
    alias_map.update(aliases or {})
    alias_map["alias"] = alias_map.get(subject) or subject
    return str(metric.expression or "").format_map(alias_map)
