from __future__ import annotations

import json
from collections.abc import Mapping
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


def _unresolved_filter_paths(
    draft: dict[str, Any],
    field: str,
) -> tuple[str, ...]:
    paths: list[str] = []
    normalized = str(field or "").rsplit(".", 1)[-1]
    for view_index, view in enumerate(draft.get("views") or []):
        if not isinstance(view, dict):
            continue
        for filter_index, item in enumerate(view.get("filters") or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("field") or "").rsplit(".", 1)[-1] != normalized:
                continue
            paths.append(
                f"/views/{view_index}/filters/{filter_index}/raw_value"
            )
    return tuple(paths)


def _field_reference_paths(
    draft: dict[str, Any],
    view_index: int,
    field: str,
) -> tuple[str, ...]:
    views = draft.get("views") or []
    if view_index < 0 or view_index >= len(views):
        return ()
    view = views[view_index]
    if not isinstance(view, dict):
        return ()
    normalized = str(field or "").rsplit(".", 1)[-1]
    if not normalized:
        return ()
    paths: list[str] = []
    for name in ("metrics", "dimensions", "output_projection"):
        values = view.get(name) or []
        if any(
            str(item or "").rsplit(".", 1)[-1] == normalized
            for item in values
        ):
            paths.append(f"/views/{view_index}/{name}")
    for index, item in enumerate(view.get("order_by") or []):
        value = (
            item.get("field") or item.get("metric")
            if isinstance(item, Mapping)
            else item
        )
        if str(value or "").rsplit(".", 1)[-1] == normalized:
            paths.append(f"/views/{view_index}/order_by/{index}")
    for index, item in enumerate(view.get("filters") or []):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("field") or "").rsplit(".", 1)[-1] == normalized:
            paths.append(f"/views/{view_index}/filters/{index}")
    return tuple(dict.fromkeys(paths))


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
    elif reason.startswith("invalid_dependency_value:"):
        evidence = dict(
            getattr(error, "dependency_evidence", {}) or {}
        )
        issue = RepairIssue(
            code="invalid_dependency_value",
            path=str(
                evidence.get("filter_path")
                or "/views/*/filters"
            ),
            reason=reason,
            expected="使用 depends_on 和结构化 inputs 表达上游依赖",
            actual=str(
                evidence.get("raw_value")
                or reason.split(":", 1)[-1].strip()
            ),
            allowed_paths=(
                "/views/*/filters",
                "/views/*/depends_on",
                "/views/*/inputs",
            ),
            evidence=evidence,
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
    elif reason.startswith("过滤值 ") and "来源为" in reason:
        field = reason.split("过滤值 ", 1)[1].split(" ", 1)[0]
        if field in mutable_technical_fields:
            issue = RepairIssue(
                code="query_spec_contract_violation",
                path="/views",
                reason=reason,
                allowed_paths=_VIEW_STRUCTURE_PATHS,
                mutable_filter_fields=mutable_technical_fields,
            )
        else:
            paths = _unresolved_filter_paths(draft, field)
            issue = RepairIssue(
                code="unresolved_provenance",
                path=paths[0] if paths else "/views/*/filters",
                reason=reason,
                expected=(
                    "只把该过滤值恢复为用户原问题中的明确值或受控解析值，"
                    "不得删除过滤条件或扩大用户范围"
                ),
                actual=field,
                allowed_paths=paths or ("/views/*/filters",),
                mutable_filter_fields=(field,),
                evidence={"field": field},
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


_AUDIT_STRUCTURE_PATHS = (
    "/views",
    "/views/*",
    "/views/*/action",
    "/views/*/view",
    "/views/*/metrics",
    "/views/*/dimensions",
    "/views/*/output_projection",
    "/views/*/order_by",
    "/views/*/limit",
    "/views/*/required_assertions",
)

_AUDIT_CODE_PATHS = {
    "missing_dimension": (
        "/views/*/dimensions",
        "/views/*/output_projection",
    ),
    "missing_metric": (
        "/views/*/metrics",
        "/views/*/output_projection",
    ),
    "invalid_view_shape": _AUDIT_STRUCTURE_PATHS,
    "unused_dependency": (
        "/views/*/depends_on",
        "/views/*/comparison_depends_on",
        "/views/*/inputs",
    ),
    "missing_input_binding": (
        "/views/*/depends_on",
        "/views/*/inputs",
        "/views/*/required_assertions",
    ),
    "lost_filter": ("/views/*/filters",),
    "wrong_grouping": (
        "/views/*/metrics",
        "/views/*/dimensions",
        "/views/*/output_projection",
    ),
    "calculation_required": (
        "/views/*/calculation",
        "/views/*/depends_on",
        "/views/*/inputs",
        "/views/*/metrics",
    ),
    "missing_derived_operand": (
        "/views/*/calculation",
        "/views/*/depends_on",
        "/views/*/inputs",
    ),
    "calculation_tool_shape_mismatch": (
        "/views/*/calculation",
        "/views/*/depends_on",
        "/views/*/inputs",
    ),
    "calculation_group_shape_mismatch": (
        "/views/*/calculation",
        "/views/*/depends_on",
        "/views/*/inputs",
    ),
    "invalid_dataset_comparison": (
        "/views/*/view",
        "/views/*/metrics",
        "/views/*/dimensions",
        "/views/*/output_projection",
        "/views/*/depends_on",
        "/views/*/comparison_depends_on",
        "/views/*/inputs",
        "/views/*/required_assertions",
    ),
}


def repair_issue_from_audit(
    problem: Mapping[str, Any],
    draft: dict[str, Any],
    registry: Any = None,
) -> RepairIssue:
    code = str(problem.get("code") or "audit_plan_incomplete").strip()
    node_id = str(problem.get("node_id") or "").strip()
    index = _view_index(draft, node_id)
    if code == "subject_conflict":
        allowed_paths = (
            f"/views/{index}/subject"
            if index is not None
            else "/views/*/subject",
        )
    else:
        allowed_paths = _AUDIT_CODE_PATHS.get(
            code,
            _AUDIT_STRUCTURE_PATHS,
        )
    issue = RepairIssue(
        code=code,
        path=f"/views/{index}" if index is not None else "/views",
        reason=str(problem.get("reason") or "").strip(),
        expected=str(problem.get("repair_hint") or "").strip(),
        actual=json.dumps(
            problem.get("set") or {},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
        allowed_values=(),
        allowed_paths=allowed_paths,
        evidence={
            "source": "independent_audit",
            "node_id": node_id,
            "audit_issue": dict(problem),
        },
    )
    return _with_scope_evidence(issue, draft)


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
    if code in _AUDIT_CODE_PATHS:
        return ((subject, _AUDIT_CODE_PATHS[code]),)
    if field_id:
        return ((subject, _VIEW_STRUCTURE_PATHS),)
    return ((subject, _VIEW_STRUCTURE_PATHS),)


def _contract_patch_path(
    raw_path: str,
    view_index: int | None,
) -> tuple[str, tuple[str, ...]]:
    if view_index is None or not raw_path:
        return "", ()
    context = raw_path.split("[", 1)[0].split(".", 1)[0]
    if not context:
        return "", ()
    suffix = (
        raw_path[len(context):]
        .replace("[", "/")
        .replace("]", "")
        .replace(".", "/")
    )
    exact_path = f"/views/{view_index}/{context}{suffix}"
    return exact_path, (f"/views/{view_index}/{context}",)


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
    evidence = getattr(failure, "evidence", {}) or {}
    evidence = evidence if isinstance(evidence, Mapping) else {}
    nested_reason = str(evidence.get("reason") or "").strip()
    if nested_reason and nested_reason not in reason:
        reason = f"{nested_reason}: {reason}"
    exact_failure_path, contract_paths = _contract_patch_path(
        str(evidence.get("path") or "").strip(),
        index,
    )
    if evidence.get("field"):
        actual = str(evidence["field"])
    paths = next(
        iter(
            _capability_paths(
                code,
                subject=subject,
                field_id=actual,
            )
        )
    )[1]
    if contract_paths:
        paths = (
            *contract_paths,
            *(
                _field_reference_paths(draft, index, actual)
                if index is not None
                else ()
            ),
        )
        paths = tuple(dict.fromkeys(paths))
    if code in {
        "dataset_comparison_requires_entity_details",
        "dataset_comparison_missing_identity",
    }:
        dependency_id = str(
            evidence.get("dependency") or ""
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
    if exact_failure_path:
        failure_path = exact_failure_path
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
