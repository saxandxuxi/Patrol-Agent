from __future__ import annotations

from typing import Any

from .json_patch import scope_signature
from .models import RepairIssue


_VIEW_STRUCTURE_PATHS = (
    "/views",
    "/views/*",
    "/views/*/action",
    "/views/*/subject",
    "/views/*/view",
    "/views/*/metrics",
    "/views/*/dimensions",
    "/views/*/filters",
    "/views/*/output_projection",
    "/views/*/order_by",
    "/views/*/limit",
    "/views/*/required_assertions",
    "/views/*/depends_on",
    "/views/*/comparison_depends_on",
    "/views/*/inputs",
    "/views/*/calculation",
)


def time_filter_fields(
    draft: dict[str, Any],
    registry: Any = None,
) -> tuple[str, ...]:
    if registry is None:
        return ()
    fields: set[str] = set()
    for view in draft.get("views") or []:
        if not isinstance(view, dict):
            continue
        subject = str(view.get("subject") or "").strip()
        subject_fields = registry.snapshot.fields.get(subject, {})
        for item in view.get("filters") or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            capability = subject_fields.get(field)
            if capability is not None and "time" in capability.roles:
                fields.add(field)
    return tuple(sorted(fields))


def technical_filter_fields(
    draft: dict[str, Any],
    registry: Any = None,
) -> tuple[str, ...]:
    if registry is None:
        return ()
    fields: set[str] = set()
    for view in draft.get("views") or []:
        if not isinstance(view, dict):
            continue
        subject = str(view.get("subject") or "").strip()
        subject_fields = registry.snapshot.fields.get(subject, {})
        for item in view.get("filters") or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            capability = subject_fields.get(field)
            if capability is not None and "soft_delete" in capability.roles:
                fields.add(field)
    return tuple(sorted(fields))


def _with_scope_evidence(
    issue: RepairIssue,
    draft: dict[str, Any],
) -> RepairIssue:
    return RepairIssue(
        code=issue.code,
        path=issue.path,
        reason=issue.reason,
        expected=issue.expected,
        actual=issue.actual,
        allowed_values=issue.allowed_values,
        preserve=issue.preserve,
        allowed_paths=issue.allowed_paths,
        mutable_filter_fields=issue.mutable_filter_fields,
        evidence={
            **issue.evidence,
            "scope_signature": scope_signature(
                draft,
                issue.mutable_filter_fields,
            ),
        },
    )


def repair_issue_from_error(
    error: Exception,
    draft: dict[str, Any],
    registry: Any = None,
) -> RepairIssue:
    reason = str(error or "").strip()
    mutable_time_fields = time_filter_fields(draft, registry)
    mutable_technical_fields = technical_filter_fields(draft, registry)
    if reason.startswith("invalid_action:"):
        issue = RepairIssue(
            code="invalid_action",
            path="/views/*/action",
            reason=reason,
            expected="query_data、post_calc、analysis 或 analyze",
            actual=reason.split(":", 1)[-1].strip(),
            allowed_values=(
                "query_data",
                "post_calc",
                "analysis",
                "analyze",
            ),
            allowed_paths=(
                "/views/*/action",
                "/views/*/required_assertions",
            ),
        )
    elif "missing_recent_window_dependency" in reason:
        issue = RepairIssue(
            code="missing_recent_window_dependency",
            path="/views",
            reason=reason,
            allowed_paths=(
                "/views",
                "/views/*",
                "/views/*/action",
                "/views/*/subject",
                "/views/*/view",
                "/views/*/metrics",
                "/views/*/dimensions",
                "/views/*/filters",
                "/views/*/output_projection",
                "/views/*/order_by",
                "/views/*/limit",
                "/views/*/required_assertions",
                "/views/*/depends_on",
                "/views/*/inputs",
            ),
        )
    elif "两个明确日期" in reason or "比较或变化" in reason:
        issue = RepairIssue(
            code="missing_date_comparison_nodes",
            path="/views",
            reason=reason,
            allowed_paths=(
                "/views",
                "/views/*",
                "/views/*/filters",
            ),
            mutable_filter_fields=mutable_time_fields,
        )
    elif "用户明确要求涉及字段" in reason:
        issue = RepairIssue(
            code="missing_required_field",
            path="/views",
            reason=reason,
            allowed_paths=(
                "/views",
                "/views/*",
                "/views/*/dimensions",
                "/views/*/output_projection",
                "/views/*/filters",
            ),
            mutable_filter_fields=mutable_time_fields,
        )
    elif "排序字段" in reason or "order_by" in reason:
        issue = RepairIssue(
            code="invalid_order_field",
            path="/views/0/order_by",
            reason=reason,
            allowed_paths=("/views/*/order_by",),
        )
    elif "主体" in reason:
        issue = RepairIssue(
            code="subject_conflict",
            path="/views/0/subject",
            reason=reason,
            allowed_paths=("/views/*/subject",),
        )
    elif "依赖" in reason or "inputs" in reason or "输入绑定" in reason:
        issue = RepairIssue(
            code="missing_dependency",
            path="/views",
            reason=reason,
            allowed_paths=(
                "/views",
                "/views/*/inputs",
                "/views/*/depends_on",
                "/views/*/required_assertions",
            ),
        )
    elif "limit" in reason:
        issue = RepairIssue(
            code="invalid_limit",
            path="/views/0/limit",
            reason=reason,
            allowed_paths=("/views/*/limit",),
        )
    else:
        issue = RepairIssue(
            code="query_spec_contract_violation",
            path="/views",
            reason=reason,
            allowed_paths=_VIEW_STRUCTURE_PATHS,
            mutable_filter_fields=mutable_technical_fields,
        )
    return _with_scope_evidence(issue, draft)


def _view_index(draft: dict[str, Any], node_id: str) -> int | None:
    for index, view in enumerate(draft.get("views") or []):
        if not isinstance(view, dict):
            continue
        if str(view.get("id") or f"q{index + 1}").strip() == node_id:
            return index
    return None


def _capability_paths(
    code: str,
    *,
    subject: str,
    field_id: str = "",
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if code in {
        "unknown_metric",
        "missing_derived_metric",
        "missing_derived_operand",
    }:
        return ((subject, ("/views/*/metrics",)),)
    if code.startswith("calculation_"):
        return (
            (
                subject,
                (
                    "/views/*/calculation",
                    "/views/*/metrics",
                    "/views/*/depends_on",
                ),
            ),
        )
    if "order" in code:
        return ((subject, ("/views/*/order_by",)),)
    if code == "missing_input_binding":
        return (
            (
                subject,
                (
                    "/views/*/depends_on",
                    "/views/*/inputs",
                    "/views/*/required_assertions",
                ),
            ),
        )
    if code.startswith("dataset_comparison"):
        return (
            (
                subject,
                (
                    "/views/*/depends_on",
                    "/views/*/comparison_depends_on",
                    "/views/*/required_assertions",
                ),
            ),
        )
    if field_id:
        return ((subject, _VIEW_STRUCTURE_PATHS),)
    return ((subject, _VIEW_STRUCTURE_PATHS),)


def repair_issue_from_capability(
    failure: Any,
    draft: dict[str, Any],
) -> RepairIssue:
    code = str(getattr(failure, "code", "") or "")
    node_id = str(getattr(failure, "node_id", "") or "").strip()
    index = _view_index(draft, node_id)
    subject = (
        f"/views/{index}"
        if index is not None
        else "/views"
    )
    allowed_values = tuple(
        str(item)
        for item in getattr(failure, "allowed_values", ()) or ()
    )
    reason = str(getattr(failure, "reason", "") or "").strip()
    actual = str(getattr(failure, "original_value", "") or "")
    paths = next(
        iter(
            _capability_paths(
                code,
                subject=subject,
                field_id=actual,
            )
        )
    )[1]
    if code in {
        "dataset_comparison_requires_entity_details",
        "dataset_comparison_missing_identity",
    }:
        evidence = getattr(failure, "evidence", {}) or {}
        dependency_id = str(
            evidence.get("dependency") or ""
            if isinstance(evidence, dict)
            else ""
        ).strip()
        dependency_index = _view_index(draft, dependency_id)
        if dependency_index is not None:
            if code == "dataset_comparison_requires_entity_details":
                paths = (
                    *paths,
                    *(
                        f"/views/{dependency_index}/{field}"
                        for field in (
                            "view",
                            "metrics",
                            "dimensions",
                            "output_projection",
                            "order_by",
                            "limit",
                        )
                    ),
                )
            else:
                paths = (
                    *paths,
                    f"/views/{dependency_index}/output_projection",
                )
    failure_path = paths[0]
    if index is not None:
        failure_path = failure_path.replace(
            "/views/*",
            f"/views/{index}",
            1,
        )
    issue = RepairIssue(
        code=code,
        path=failure_path,
        reason=reason,
        expected="、".join(allowed_values),
        actual=actual,
        allowed_values=allowed_values,
        allowed_paths=paths,
        evidence={
            "source": str(getattr(failure, "source", "") or ""),
            "node_id": node_id,
            "suggested_repair": str(
                getattr(failure, "suggested_repair", "") or ""
            ),
            "failure_evidence": (
                failure.to_feedback()
                if hasattr(failure, "to_feedback")
                else {}
            ),
        },
    )
    return _with_scope_evidence(issue, draft)
