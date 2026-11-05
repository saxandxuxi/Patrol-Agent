from __future__ import annotations

from .contracts import QuerySpec, VerificationIssue
from .fields import OutputProjection, PhysicalSchemaView


_SUPPORTED_VIEWS = {
    "count",
    "sum",
    "avg",
    "distribution",
    "list",
    "detail",
    "top",
    "trend",
    "bucket_compare",
    "photo",
    "data",
}


def validate_query_spec(
    spec: QuerySpec,
    schema: PhysicalSchemaView,
    projection: OutputProjection,
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
        if item.source != "user_explicit" and not item.resolver.strip():
            issues.append(
                VerificationIssue(
                    code="unresolved_provenance",
                    message=f"过滤值 {item.field} 来源为 {item.source}，但缺少解析器",
                    node_id=spec.id,
                    stage="contract",
                )
            )
    return tuple(issues)
