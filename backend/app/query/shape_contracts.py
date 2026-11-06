from __future__ import annotations

from dataclasses import dataclass, replace

from .capability_registry import CapabilityRegistry
from .contracts import QuerySpec, SUPPORTED_QUERY_VIEWS


@dataclass(frozen=True)
class ContractIssue:
    code: str
    reason: str
    node_id: str


def _base_field(field: str) -> str:
    return str(field or "").rsplit(".", 1)[-1]


def normalize_top_view(spec: QuerySpec) -> QuerySpec:
    if spec.view != "top":
        return spec
    return replace(
        spec,
        view="top_groups" if spec.metrics else "top_records",
    )


def _issue(spec: QuerySpec, code: str, reason: str) -> ContractIssue:
    return ContractIssue(code=code, reason=reason, node_id=spec.id)


def validate_shape_contract(
    spec: QuerySpec,
    registry: CapabilityRegistry | None = None,
) -> tuple[ContractIssue, ...]:
    registry = registry or CapabilityRegistry.load()
    if spec.view == "top":
        return (
            _issue(
                spec,
                "ambiguous_top_view",
                "top 必须先归一化为 top_records 或 top_groups",
            ),
        )
    if spec.view not in SUPPORTED_QUERY_VIEWS:
        return (
            _issue(
                spec,
                "unsupported_view",
                f"不支持的查询视图：{spec.view}",
            ),
        )
    subject = registry.snapshot.subjects.get(spec.subject)
    subject_views = set(subject.allowed_views if subject else ())
    if spec.view in {"top_records", "top_groups"} and spec.view not in subject_views:
        return (
            _issue(
                spec,
                "unsupported_view",
                f"主体 {spec.subject} 不支持查询视图 {spec.view}",
            ),
        )
    issues: list[ContractIssue] = []
    if spec.view == "top_records":
        if spec.metrics:
            issues.append(
                _issue(
                    spec,
                    "metrics_not_allowed",
                    "top_records 返回明细记录，不能声明聚合指标",
                )
            )
        if spec.dimensions:
            issues.append(
                _issue(
                    spec,
                    "dimensions_not_allowed",
                    "top_records 返回逐条记录，不能声明分组维度",
                )
            )
        if not spec.output_projection:
            issues.append(
                _issue(
                    spec,
                    "missing_output_projection",
                    "top_records 必须声明要返回的明细字段",
                )
            )
        if not spec.order_by:
            issues.append(
                _issue(
                    spec,
                    "missing_order_by",
                    "top_records 必须声明排序字段",
                )
            )
        if spec.output_projection:
            projected = {_base_field(item) for item in spec.output_projection}
            for item in spec.order_by:
                if not isinstance(item, dict):
                    continue
                field = _base_field(str(item.get("field") or ""))
                if field and field not in projected:
                    issues.append(
                        _issue(
                            spec,
                            f"order_field_not_projected:{field}",
                            f"排序字段 {field} 必须同时出现在返回字段中",
                        )
                    )
    elif spec.view == "top_groups":
        if not spec.metrics:
            issues.append(
                _issue(
                    spec,
                    "missing_metrics",
                    "top_groups 必须声明用于排名的聚合指标",
                )
            )
        if not spec.dimensions:
            issues.append(
                _issue(
                    spec,
                    "missing_dimensions",
                    "top_groups 必须声明分组维度",
                )
            )
        rank_fields = {
            *(_base_field(item) for item in spec.metrics),
            *(_base_field(item) for item in spec.dimensions),
        }
        for item in spec.order_by:
            if not isinstance(item, dict):
                continue
            field = _base_field(str(item.get("field") or ""))
            if field and field not in rank_fields:
                issues.append(
                    _issue(
                        spec,
                        f"invalid_order_field:{field}",
                        f"top_groups 排序字段 {field} 必须来自指标或分组维度",
                    )
                )
    if spec.view in {"top_records", "top_groups"} and (
        spec.limit is None or int(spec.limit) <= 0
    ):
        issues.append(
            _issue(
                spec,
                "missing_limit",
                "TopN 查询必须声明正整数 limit",
            )
        )
    return tuple(issues)
