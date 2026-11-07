from __future__ import annotations

from .contracts import QuerySpec, SUPPORTED_QUERY_VIEWS, VerificationIssue
from .fields import OutputProjection, PhysicalSchemaView


_SUPPORTED_VIEWS = set(SUPPORTED_QUERY_VIEWS)
_SUPPORTED_OPS = {
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "between",
    "is_null",
    "not_null",
}


def validate_query_spec(
    spec: QuerySpec,
    schema: PhysicalSchemaView,
    projection: OutputProjection,
    *,
    node_type: str = "query_data",
) -> tuple[VerificationIssue, ...]:
    issues: list[VerificationIssue] = []
    if spec.view not in _SUPPORTED_VIEWS:
        issues.append(
            VerificationIssue(
                code="unsupported_view",
                message=f"不支持的查询视图：{spec.view}",
                node_id=spec.id,
                stage="contract",
            )
        )
    # 分布视图的分组维度是问题的范围本身：丢掉维度会把整表聚合成一行，
    # 相当于替用户换了问题。宁可在执行前拦住去修，也不能静默给总量。
    if (
        node_type == "query_data"
        and spec.view == "distribution"
        and not spec.dimensions
    ):
        issues.append(
            VerificationIssue(
                code="missing_dimension",
                message=(
                    "分布查询必须声明分组维度，否则结果只有一行总量，"
                    "不能算回答分布问题"
                ),
                node_id=spec.id,
                stage="contract",
            )
        )
    # 逐条记录的视图不聚合。声明了指标却落在这些视图上，指标会被编译器丢掉，
    # 用户问“总面积”只会拿到明细行，必须执行前拦住去修。
    if (
        node_type == "query_data"
        and spec.view in {"list", "detail", "data"}
        and spec.metrics
    ):
        issues.append(
            VerificationIssue(
                code="metrics_require_aggregate_view",
                message=(
                    f"视图 {spec.view} 不聚合，声明的指标"
                    f"（{'、'.join(spec.metrics)}）不会被执行；"
                    "请改用 count / sum / avg / distribution / top_groups"
                ),
                node_id=spec.id,
                stage="contract",
            )
        )
    for field in projection.fields:
        if not field.display_label.strip():
            issues.append(
                VerificationIssue(
                    code="missing_display_label",
                    message=f"输出字段 {field.table}.{field.column} 缺少展示标签",
                    node_id=spec.id,
                    stage="contract",
                )
            )
        if not schema.has_column(field.table, field.column):
            issues.append(
                VerificationIssue(
                    code="unknown_physical_field",
                    message=f"物理字段不存在：{field.table}.{field.column}",
                    node_id=spec.id,
                    stage="contract",
                )
            )
    for item in spec.filters:
        if item.op not in _SUPPORTED_OPS:
            issues.append(
                VerificationIssue(
                    code="unsupported_operator",
                    message=f"过滤字段 {item.field} 使用了不支持的运算符：{item.op}",
                    node_id=spec.id,
                    stage="contract",
                )
            )
        resolver = item.resolver.strip()
        inferred_identity = (
            item.source == "model_inference"
            and resolver.startswith("identity:")
        )
        if item.source != "user_explicit" and (not resolver or inferred_identity):
            issues.append(
                VerificationIssue(
                    code="unresolved_provenance",
                    message=(
                        f"过滤值 {item.field} 来源为 {item.source}，"
                        "但没有经过字典、业务规则或受控解析"
                    ),
                    node_id=spec.id,
                    stage="contract",
                )
            )
    return tuple(issues)
