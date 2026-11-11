from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import re
from typing import Any

import sqlglot
from sqlglot import exp

from ..metrics.semantics import aggregation_from_expression
from .loader import CapabilityConfigError
from .models import MetricCapability


def inherited_cube_metrics(
    mdl: Mapping[str, Any],
    subject_by_model: Mapping[str, str],
) -> dict[str, dict[str, MetricCapability]]:
    metrics: dict[str, dict[str, MetricCapability]] = {}
    for cube in mdl.get("cubes") or ():
        if not isinstance(cube, Mapping):
            continue
        subject = subject_by_model.get(str(cube.get("base_object") or ""))
        if not subject:
            continue
        for measure in cube.get("measures") or ():
            if not isinstance(measure, Mapping):
                continue
            metric_id = str(measure.get("name") or "")
            expression = _qualify_columns(str(measure.get("expression") or ""))
            if not metric_id or not expression:
                continue
            metric = MetricCapability(
                metric_id=metric_id,
                expression=expression,
                label=str(
                    (measure.get("properties") or {}).get(
                        "description"
                    )
                    or metric_id
                ),
                allowed_views=_allowed_views(expression),
                aggregation=aggregation_from_expression(expression),
                grain=subject,
                identity=f"{subject}.id",
                fanout_safe=False,
                source_kind="mdl_cube",
                source_reference=(
                    f"{str(cube.get('name') or '')}.{metric_id}"
                ),
            )
            existing = metrics.get(subject, {}).get(metric_id)
            metrics.setdefault(subject, {})[metric_id] = (
                merge_metric_facts(
                    existing,
                    metric,
                    subject=subject,
                    metric_id=metric_id,
                    existing_source="mdl",
                    incoming_source="mdl",
                )
                if existing is not None
                else metric
            )
    return metrics


def merge_metric_facts(
    existing: MetricCapability,
    incoming: MetricCapability,
    *,
    subject: str,
    metric_id: str,
    existing_source: str,
    incoming_source: str,
) -> MetricCapability:
    """Merge metric facts only when their executable meaning agrees.

    Metric ids are shared by local business capabilities and MDL cubes. A
    same-id metric is one capability, not two competing definitions. Keep
    compatible presentation metadata (labels, views, joins) together and
    reject incompatible formulas instead of silently choosing one source.
    """

    if _expression_identity(existing.expression) != _expression_identity(
        incoming.expression
    ):
        evidence = {
            "code": "conflicting_metric_fact",
            "subject": subject,
            "metric_id": metric_id,
            "sources": [existing_source, incoming_source],
            "existing": _metric_evidence(existing),
            "incoming": _metric_evidence(incoming),
            "reason": (
                "同一指标 id 在不同事实源中具有不同 SQL 表达式；"
                "不能静默选择一个来源。"
            ),
        }
        raise CapabilityConfigError(
            f"conflicting_metric_fact: {subject}.{metric_id}",
            evidence=evidence,
        )

    if existing.metric_id != incoming.metric_id:
        existing_declares_alias = incoming.metric_id in existing.aliases
        incoming_declares_alias = existing.metric_id in incoming.aliases
        if not existing_declares_alias and not incoming_declares_alias:
            evidence = {
                "code": "undeclared_metric_alias",
                "subject": subject,
                "metric_id": metric_id,
                "sources": [existing_source, incoming_source],
                "existing": _metric_evidence(existing),
                "incoming": _metric_evidence(incoming),
                "reason": (
                    "两个指标 id 的公式相同，但没有显式登记别名关系；"
                    "不能静默归并为同一个指标。"
                ),
            }
            raise CapabilityConfigError(
                f"undeclared_metric_alias: {subject}.{metric_id}",
                evidence=evidence,
            )

    if (
        existing.unit
        and incoming.unit
        and _unit_identity(existing.unit)
        != _unit_identity(incoming.unit)
    ):
        evidence = {
            "code": "conflicting_metric_fact",
            "subject": subject,
            "metric_id": metric_id,
            "sources": [existing_source, incoming_source],
            "existing": _metric_evidence(existing),
            "incoming": _metric_evidence(incoming),
            "reason": "同一指标 id 的单位不一致。",
        }
        raise CapabilityConfigError(
            f"conflicting_metric_fact: {subject}.{metric_id}",
            evidence=evidence,
        )

    semantic_conflicts = {
        name: (getattr(existing, name), getattr(incoming, name))
        for name in ("aggregation", "grain", "identity")
        if getattr(existing, name)
        and getattr(incoming, name)
        and getattr(existing, name) != getattr(incoming, name)
    }
    if (
        existing.aggregation
        and incoming.aggregation
        and existing.fanout_safe != incoming.fanout_safe
    ):
        semantic_conflicts["fanout_safe"] = (
            existing.fanout_safe,
            incoming.fanout_safe,
        )
    if semantic_conflicts:
        evidence = {
            "code": "conflicting_metric_semantics",
            "subject": subject,
            "metric_id": metric_id,
            "sources": [existing_source, incoming_source],
            "existing": _metric_evidence(existing),
            "incoming": _metric_evidence(incoming),
            "conflicts": semantic_conflicts,
        }
        raise CapabilityConfigError(
            f"conflicting_metric_semantics: {subject}.{metric_id}",
            evidence=evidence,
        )

    if incoming.metric_id in existing.aliases:
        canonical, alias_fact = existing, incoming
    elif existing.metric_id in incoming.aliases:
        canonical, alias_fact = incoming, existing
    else:
        canonical, alias_fact = existing, incoming
    aliases = tuple(
        dict.fromkeys(
            (
                *canonical.aliases,
                *alias_fact.aliases,
                *(
                    (
                        alias_fact.metric_id,
                    )
                    if alias_fact.metric_id != canonical.metric_id
                    else ()
                ),
            )
        )
    )
    return replace(
        canonical,
        label=canonical.label or alias_fact.label,
        unit=canonical.unit or alias_fact.unit,
        aliases=aliases,
        allowed_views=tuple(
            dict.fromkeys(
                (
                    *canonical.allowed_views,
                    *alias_fact.allowed_views,
                )
            )
        ),
        requires_join=tuple(
            dict.fromkeys(
                (
                    *canonical.requires_join,
                    *alias_fact.requires_join,
                )
            )
        ),
        propagate_across_groups=(
            canonical.propagate_across_groups
            or alias_fact.propagate_across_groups
        ),
        aggregation=(
            canonical.aggregation or alias_fact.aggregation
        ),
        grain=canonical.grain or alias_fact.grain,
        identity=canonical.identity or alias_fact.identity,
        fanout_safe=(
            canonical.fanout_safe
            if canonical.aggregation
            else alias_fact.fanout_safe
        ),
        source_kind=(
            canonical.source_kind or alias_fact.source_kind
        ),
        source_reference=(
            canonical.source_reference
            or alias_fact.source_reference
        ),
    )


def _expression_identity(expression: str) -> str:
    text = str(expression or "").strip()
    if not text:
        return ""
    text = re.sub(r"\{([^{}]+)\}", r"\1", text)
    try:
        parsed = sqlglot.parse_one(f"SELECT {text}", read="mysql")
        for column in parsed.find_all(exp.Column):
            column.set("table", None)
        return " ".join(
            parsed.expressions[0].sql(dialect="mysql").lower().split()
        )
    except (sqlglot.errors.ParseError, IndexError, AttributeError):
        return " ".join(text.lower().split())


def _unit_identity(unit: str) -> str:
    normalized = str(unit or "").strip().lower().replace(" ", "")
    return {
        "m": "米",
        "meter": "米",
        "meters": "米",
        "km": "公里",
        "kilometer": "公里",
        "kilometers": "公里",
        "㎡": "平方米",
        "m2": "平方米",
        "m²": "平方米",
    }.get(normalized, normalized)


def _metric_evidence(metric: MetricCapability) -> dict[str, Any]:
    return {
        "metric_id": metric.metric_id,
        "expression": metric.expression,
        "label": metric.label,
        "unit": metric.unit,
        "aliases": list(metric.aliases),
        "allowed_views": list(metric.allowed_views),
        "requires_join": list(metric.requires_join),
        "propagate_across_groups": metric.propagate_across_groups,
        "aggregation": metric.aggregation,
        "grain": metric.grain,
        "identity": metric.identity,
        "fanout_safe": metric.fanout_safe,
        "source_kind": metric.source_kind,
        "source_reference": metric.source_reference,
    }


def _qualify_columns(expression: str) -> str:
    if not expression.strip():
        return ""
    try:
        parsed = sqlglot.parse_one(
            f"SELECT {expression}",
            read="mysql",
        )
    except sqlglot.errors.ParseError:
        return ""
    qualified = parsed.transform(
        lambda node: (
            exp.Column(
                this=node.this,
                table=exp.Identifier(this="{alias}"),
            )
            if isinstance(node, exp.Column) and not node.table
            else node
        )
    )
    if len(qualified.expressions) != 1:
        return ""
    return qualified.expressions[0].sql(dialect="mysql")


def _allowed_views(expression: str) -> tuple[str, ...]:
    parsed = sqlglot.parse_one(
        f"SELECT {expression}",
        read="mysql",
    )
    aggregate = parsed.expressions[0]
    if isinstance(aggregate, (exp.Sum, exp.Avg)):
        return (
            "count",
            "sum" if isinstance(aggregate, exp.Sum) else "avg",
            "distribution",
            "top_groups",
            "trend",
        )
    return ("count", "distribution", "top_groups", "trend")
