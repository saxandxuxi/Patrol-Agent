from __future__ import annotations

from dataclasses import dataclass, replace

from .capability_registry import CapabilityRegistry
from .contracts import QuerySpec, PUBLIC_QUERY_VIEWS
from .pile_segments import is_pile_segment_field


_VIEW_ALIASES = {
    "group": "distribution",
    "grouped": "distribution",
    "aggregate": "distribution",
    "summary": "distribution",
}

# 模型偶尔会自造“分析/图表”这类视图名。它们是表达方式，不是新能力：
# 按有没有指标和维度落到已有视图，避免把内部视图名当成数据能力缺失。
_ANALYSIS_VIEW_ALIASES = frozenset(
    {
        "analysis",
        "analysis_only",
        "insight",
        "insights",
        "statistics",
        "stats",
        "chart",
        "graph",
        "plot",
        "visual",
        "visualization",
        "report",
    }
)

# 这几个视图返回逐条记录，不聚合。声明了指标还落在这些视图上，
# 指标会被编译器静默丢掉（用户要“总面积”，拿到的却是明细行）。
_RECORD_VIEWS = frozenset({"list", "detail", "data"})
_SEGMENT_IMPLIED_OUTPUTS = frozenset({"start_pile", "end_pile"})


def canonical_view(
    value: object,
    metrics: tuple[str, ...] = (),
    dimensions: tuple[str, ...] = (),
    node_type: str = "query_data",
) -> str:
    view = str(value or "").strip()
    if view in {"table", "table_view"}:
        return "distribution" if metrics and dimensions else "list"
    view = _VIEW_ALIASES.get(view, view)
    if view in _ANALYSIS_VIEW_ALIASES:
        if node_type == "post_calc":
            return "data"
        if metrics and dimensions:
            return "distribution"
        return "count" if metrics else "list"
    if view in _RECORD_VIEWS and metrics and node_type != "post_calc":
        return "distribution" if dimensions else "count"
    if view == "distribution" and metrics and not dimensions:
        # 没有分组维度的"分布"就是一行全局聚合（多少处、总面积、总长度），
        # 与 count/sum/avg 视图同义。模型写 distribution 时按聚合视图执行，
        # 不再用"分布必须声明分组维度"把整问拦死；用户真要分组时，
        # 审计（audit_query_plan）仍会按"计划没回答分布问题"打回重排。
        return "count"
    return view


@dataclass(frozen=True)
class ContractIssue:
    code: str
    reason: str
    node_id: str


def _base_field(field: str) -> str:
    return str(field or "").rsplit(".", 1)[-1]


def normalize_top_view(spec: QuerySpec) -> QuerySpec:
    if spec.view not in {"top", "top_records", "top_groups"}:
        return spec
    if spec.limit is None or int(spec.limit) <= 0:
        if spec.metrics:
            return replace(
                spec,
                view="distribution" if spec.dimensions else "count",
            )
        return replace(spec, view="list")
    if spec.view != "top":
        return spec
    return replace(
        spec,
        view="top_groups" if spec.metrics else "top_records",
    )


def normalize_post_calc_shape(spec: QuerySpec) -> QuerySpec:
    """Keep explanatory post-calc nodes on the executable data contract."""

    spec = normalize_top_view(spec)
    if (
        spec.calculation is None
        and "analysis_only" in spec.required_assertions
        and spec.view not in {"data", "trend"}
    ):
        return replace(spec, view="data")
    return spec


def normalize_segment_projection(spec: QuerySpec) -> QuerySpec:
    """Use the segment label's built-in boundaries instead of fake columns."""

    segment_dimensions = {
        _base_field(field)
        for field in spec.dimensions
        if is_pile_segment_field(field)
    }
    if not segment_dimensions:
        return spec
    dimensions = tuple(
        field
        for field in spec.dimensions
        if _base_field(field) not in _SEGMENT_IMPLIED_OUTPUTS
    )
    output_projection = tuple(
        field
        for field in spec.output_projection
        if _base_field(field) not in _SEGMENT_IMPLIED_OUTPUTS
    )
    if (
        dimensions == spec.dimensions
        and output_projection == spec.output_projection
    ):
        return spec
    return replace(
        spec,
        dimensions=dimensions,
        output_projection=output_projection,
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
    if spec.view not in PUBLIC_QUERY_VIEWS:
        return (
            _issue(
                spec,
                "unsupported_view",
                f"不支持的查询视图：{spec.view}",
            ),
        )
    if spec.view in _RECORD_VIEWS and spec.metrics:
        return (
            _issue(
                spec,
                "metrics_require_aggregate_view",
                (
                    f"视图 {spec.view} 只返回逐条记录，声明的指标"
                    f"（{'、'.join(spec.metrics)}）不会被执行；"
                    "请改用 count / sum / avg / distribution / top_groups"
                ),
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
    if (
        "top_per_outer_group" in spec.required_assertions
        and spec.view != "top_groups"
    ):
        issues.append(
            _issue(
                spec,
                "top_per_outer_group_requires_top_groups",
                "top_per_outer_group 只能用于 top_groups 视图",
            )
        )
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
        if (
            "top_per_outer_group" in spec.required_assertions
            and len(spec.dimensions) < 2
        ):
            issues.append(
                _issue(
                    spec,
                    "missing_outer_group_dimension",
                    "top_per_outer_group 必须按“外层分组维度、组内排名维度”"
                    "至少声明两个维度",
                )
            )
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
                (
                    "TopN 查询必须声明正整数 limit；如果用户只是要求按指标"
                    "排序并查看全部对象，应使用 distribution 或 list，"
                    "保留 order_by 且不要设置 limit"
                ),
            )
        )
    return tuple(issues)
