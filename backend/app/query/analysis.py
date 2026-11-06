from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..services import prompts
from ..services.llm import LLMError, chat_json, role_model
from .audit import audit_query_plan
from .capability_registry import CapabilityIssue, CapabilityRegistry
from .contracts import (
    InputBinding,
    IntentContract,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecAmbiguity,
    QuerySpecError,
    ValueBinding,
)
from .fields import FieldReachSet, OutputProjection, PhysicalSchemaView
from .planner import build_query_plan
from .spec_mdl import UnsupportedQuerySpec, compile_spec_mdl
from .validator import validate_query_spec
from .values import resolve_value


def _text_context(value: Any, limit: int = 8000) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:limit]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _metric_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            metric = item.strip()
        elif isinstance(item, dict):
            aggregate = str(item.get("agg") or item.get("aggregation") or "").lower()
            field = str(item.get("field") or item.get("column") or "").strip()
            if aggregate == "count":
                metric = "count"
            elif aggregate in {"avg", "average"} and field:
                metric = f"average_{field}"
            elif aggregate == "sum" and field:
                metric = f"sum_{field}"
            else:
                metric = field
        else:
            metric = ""
        if "(" in metric and metric.endswith(")"):
            aggregate, _, field = metric.partition("(")
            aggregate = aggregate.strip().lower()
            field = field[:-1].strip().lower()
            if aggregate == "count":
                metric = "count"
            elif aggregate == "sum":
                metric = {
                    "area": "sum_area",
                    "disease_length": "sum_disease_length",
                    "effective_disease_length": "sum_effective_disease_length",
                }.get(field, f"sum_{field}")
            elif aggregate in {"avg", "average"}:
                metric = {
                    "area": "average_area",
                    "disease_length": "average_length",
                    "effective_disease_length": "average_length",
                }.get(field, f"average_{field}")
        metric = _METRIC_ALIASES.get(metric, metric)
        if metric and metric not in result:
            result.append(metric)
    return tuple(result)


_METRIC_ALIASES = {
    "sum_length": "sum_disease_length",
    "total_length": "sum_disease_length",
    "avg_length": "average_length",
    "sum_effective_length": "sum_effective_disease_length",
}


_VIEW_ALIASES = {
    "group": "distribution",
    "grouped": "distribution",
    "aggregate": "distribution",
    "summary": "distribution",
}


def _canonical_view(
    value: Any,
    metrics: tuple[str, ...] = (),
    dimensions: tuple[str, ...] = (),
) -> str:
    view = str(value or "").strip()
    if view in {"table", "table_view"}:
        return "distribution" if metrics and dimensions else "list"
    return _VIEW_ALIASES.get(view, view)


def _order_items(value: Any) -> tuple[Any, ...]:
    result: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        if "direction" not in normalized:
            order = normalized.get("order")
            if order:
                normalized["direction"] = str(order)
        result.append(normalized)
    return tuple(result)


def _canonical_dimensions(subject: str, dimensions: tuple[str, ...]) -> tuple[str, ...]:
    normalized_dimensions = []
    seen: set[str] = set()
    for item in dimensions:
        compact = item.replace(" ", "").lower()
        if compact.startswith("date_format(") and any(
            field in compact
            for field in ("create_time", "collection_time", "collection_month", "create_month")
        ):
            item = "month"
        normalized = (
            "equipment_id"
            if subject == "patrol"
            and item in {"device_code", "device_id", "device", "equipment"}
            else "route_name"
            if subject == "disease" and item == "routes_id"
            else item
        )
        if normalized not in seen:
            seen.add(normalized)
            normalized_dimensions.append(normalized)
    return tuple(normalized_dimensions)


def _canonicalize_input_bindings(specs: list[QuerySpec]) -> None:
    by_id = {spec.id: spec for spec in specs}
    for index, spec in enumerate(specs):
        bindings = []
        for item in spec.input_bindings:
            from_field = str(item.from_field).rsplit(".", 1)[-1]
            to_field = str(item.to_field).rsplit(".", 1)[-1]
            source = by_id.get(item.from_node)
            source_fields = {
                str(field).rsplit(".", 1)[-1]
                for field in (
                    (*source.dimensions, *source.metrics, *source.output_projection)
                    if source is not None
                    else ()
                )
            }
            target_fields = {
                str(field).rsplit(".", 1)[-1]
                for field in (
                    *spec.dimensions,
                    *spec.filters,
                    *spec.metrics,
                    *spec.output_projection,
                )
                if not isinstance(field, ValueBinding)
            }
            if (
                from_field == "routes_id"
                and "route_name" in source_fields
            ):
                from_field = "route_name"
            if (
                to_field == "routes_id"
                and "route_name" in target_fields
            ):
                to_field = "route_name"
            bindings.append(
                InputBinding(
                    from_node=item.from_node,
                    from_field=from_field,
                    to_field=to_field,
                    op=item.op,
                )
            )
        specs[index] = QuerySpec(
            **{**spec.__dict__, "input_bindings": tuple(bindings)}
        )


def _plan_subject_issue(plan: QueryPlan, subject: str) -> str:
    if not subject:
        return ""
    nodes_by_id = {node.id: node for node in plan.nodes}
    consumed = {
        dependency
        for node in plan.nodes
        for dependency in node.depends_on
    }
    consumed.update(
        binding.from_node
        for node in plan.nodes
        for binding in node.spec.input_bindings
    )
    for node in plan.nodes:
        if node.id in consumed:
            continue
        effective_subject = node.spec.subject
        if node.node_type == "post_calc":
            dependency_subjects = {
                nodes_by_id[dependency].spec.subject
                for dependency in node.depends_on
                if dependency in nodes_by_id
            }
            if len(dependency_subjects) == 1:
                effective_subject = next(iter(dependency_subjects))
        if effective_subject != subject:
            return (
                f"最终回答主体 {effective_subject} 与已确定主体 {subject} 不一致"
            )
    return ""


def _plan_intent_issue(plan: QueryPlan, intent_family: str) -> str:
    if intent_family != "trend":
        return ""
    trend_nodes = [
        node
        for node in plan.nodes
        if node.node_type == "post_calc" and node.spec.view == "trend"
    ]
    if trend_nodes:
        return ""
    return "趋势问题必须增加一个消费数据结果的 post_calc 节点，并使用 view=trend"


def _complete_trend_dependencies(plan: QueryPlan) -> QueryPlan:
    data_ids = tuple(node.id for node in plan.nodes if node.node_type == "query_data")
    if not data_ids:
        return plan
    nodes = []
    for node in plan.nodes:
        if node.node_type == "post_calc" and node.spec.view == "trend":
            node = PlanNode(
                id=node.id,
                spec=node.spec,
                depends_on=tuple(
                    dict.fromkeys((*data_ids, *node.depends_on))
                ),
                node_type=node.node_type,
            )
        nodes.append(node)
    return QueryPlan(
        question=plan.question,
        nodes=tuple(nodes),
        response_mode=plan.response_mode,
    )


def _analyze_once(
    question: str,
    kind: str,
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
    planning_feedback: str = "",
    *,
    registry: CapabilityRegistry | None = None,
    subject: str = "",
) -> QueryPlan:
    original = str(question or "").strip()
    if not original:
        raise QuerySpecError("用户问题为空")
    planner_view = (
        registry.planner_view(subject, reach).to_prompt_dict()
        if registry is not None and subject
        else {}
    )
    data = chat_json(
        prompts.messages(
            "query_spec",
            question=original,
            kind=kind,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            capabilities=_text_context(planner_view, limit=24000),
            schema=_text_context(
                sorted(
                    (table, column)
                    for table, column in schema.columns
                    if planner_view
                    and table == planner_view.get("base_table")
                ),
                limit=5000,
            )
            if planner_view
            else _text_context(sorted(schema.columns)),
            planning_feedback=planning_feedback or "无",
            reach=_text_context(
                [
                    {
                        "table": item.table,
                        "column": item.column,
                        "matched_text": item.matched_text,
                        "source": item.source,
                        "score": item.score,
                    }
                    for item in reach.hits
                ]
            ),
        ),
        model=role_model("semantic"),
    )
    if not isinstance(data, dict):
        raise QuerySpecError("模型没有返回 QuerySpec JSON")
    contract = IntentContract(
        original_question=original,
        kind=kind,
        intent_family=str(data.get("intent_family") or "").strip(),
        final_goal=str(data.get("final_goal") or original).strip(),
        unknown_terms=_string_tuple(data.get("unknown_terms")),
    )
    specs: list[QuerySpec] = []
    dependencies: dict[str, tuple[str, ...]] = {}
    node_types: dict[str, str] = {}
    raw_views = data.get("views")
    if not isinstance(raw_views, list):
        raise QuerySpecError("QuerySpec 缺少 views")
    for index, raw in enumerate(raw_views, 1):
        if not isinstance(raw, dict):
            continue
        spec_id = str(raw.get("id") or f"q{index}").strip()
        selected_subject = str(raw.get("subject") or subject or "disease").strip()
        filters = []
        for item in raw.get("filters") or []:
            if not isinstance(item, dict):
                raise QuerySpecError("过滤条件必须是对象")
            filters.append(
                resolve_value(
                    str(item.get("field") or ""),
                    str(item.get("op") or "="),
                    item.get("raw_value", item.get("value")),
                    original,
                    kind,
                    subject=selected_subject,
                    registry=registry,
                )
            )
        try:
            limit = int(raw["limit"]) if raw.get("limit") is not None else None
        except (TypeError, ValueError) as exc:
            raise QuerySpecError("limit 必须是整数") from exc
        action = str(raw.get("action") or "query_data").strip()
        if action == "analysis":
            action = "post_calc"
        node_type = (
            action
            if action in {"query_data", "post_calc", "analysis"}
            else "query_data"
        )
        metrics = _metric_tuple(raw.get("metrics"))
        dimensions = _canonical_dimensions(
            selected_subject,
            _string_tuple(raw.get("dimensions")),
        )
        view = _canonical_view(raw.get("view"), metrics, dimensions)
        if (
            node_type == "post_calc"
            and view in {"metrics", "derived_metrics", "post_calc"}
        ):
            view = "data"
        if view in {"list", "detail"} and limit is None:
            limit = 500
        output_projection = _canonical_dimensions(
            selected_subject,
            _string_tuple(raw.get("output_projection")),
        )
        input_bindings = []
        for item in raw.get("inputs") or []:
            if not isinstance(item, dict):
                raise QuerySpecError("inputs 必须是对象数组")
            input_bindings.append(
                InputBinding(
                    from_node=str(item.get("from_node") or "").strip(),
                    from_field=str(item.get("from_field") or "").strip(),
                    to_field=str(item.get("to_field") or "").strip(),
                    op=str(item.get("op") or "in").strip(),
                )
            )
        specs.append(
            QuerySpec(
                id=spec_id,
                kind=kind,
                subject=selected_subject,
                view=view,
                metrics=metrics,
                dimensions=dimensions,
                filters=tuple(filters),
                output_projection=output_projection,
                order_by=_order_items(raw.get("order_by")),
                limit=limit,
                required_assertions=_string_tuple(raw.get("required_assertions")),
                question=str(raw.get("question") or original).strip(),
                input_bindings=tuple(input_bindings),
            )
        )
        dependencies[spec_id] = _string_tuple(raw.get("depends_on"))
        node_types[spec_id] = node_type
    _canonicalize_input_bindings(specs)
    plan = build_query_plan(
        contract,
        tuple(specs),
        schema,
        reach,
        dependencies,
        node_types,
        response_mode=(
            "analysis"
            if contract.intent_family in {"analysis", "trend"}
            else "data"
        ),
    )
    plan = _complete_trend_dependencies(plan)
    subject_issue = _plan_subject_issue(plan, subject)
    if subject_issue:
        raise QuerySpecError(subject_issue)
    intent_issue = _plan_intent_issue(plan, contract.intent_family)
    if intent_issue:
        raise QuerySpecError(intent_issue)
    return plan


def _apply_audit_patches(plan: QueryPlan, verdict: dict[str, Any]) -> QueryPlan:
    allowed = {
        "limit",
        "view",
        "metrics",
        "dimensions",
        "order_by",
        "output_projection",
        "required_assertions",
    }
    patch_by_node: dict[str, dict[str, Any]] = {}
    for issue in verdict.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        node_id = str(issue.get("node_id") or "")
        changes = issue.get("set")
        if node_id and isinstance(changes, dict):
            patch_by_node.setdefault(node_id, {}).update(
                {
                    key: value
                    for key, value in changes.items()
                    if key in allowed
                }
            )
    if not patch_by_node:
        return plan
    nodes = []
    for node in plan.nodes:
        changes = patch_by_node.get(node.id)
        if not changes:
            nodes.append(node)
            continue
        values = dict(node.spec.__dict__)
        values.update(changes)
        if "metrics" in changes:
            values["metrics"] = _metric_tuple(changes["metrics"])
        if "dimensions" in changes:
            values["dimensions"] = _canonical_dimensions(
                str(values.get("subject") or ""),
                _string_tuple(changes["dimensions"]),
            )
        if "output_projection" in changes:
            values["output_projection"] = _canonical_dimensions(
                str(values.get("subject") or ""),
                _string_tuple(changes["output_projection"]),
            )
        if "order_by" in changes:
            values["order_by"] = _order_items(changes["order_by"])
        if "required_assertions" in changes:
            values["required_assertions"] = _string_tuple(
                changes["required_assertions"]
            )
        if "view" in changes:
            values["view"] = _canonical_view(
                changes["view"],
                tuple(values.get("metrics") or ()),
                tuple(values.get("dimensions") or ()),
            )
        nodes.append(
            node.__class__(
                id=node.id,
                spec=QuerySpec(**values),
                depends_on=node.depends_on,
                node_type=node.node_type,
            )
        )
    return QueryPlan(
        question=plan.question,
        nodes=tuple(nodes),
        response_mode=plan.response_mode,
    )


def _plan_contract_issues(
    plan: QueryPlan,
    schema: PhysicalSchemaView,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for node in plan.nodes:
        for issue in validate_query_spec(
            node.spec,
            schema,
            OutputProjection(fields=()),
        ):
            issues.append(
                {
                    "code": issue.code,
                    "node_id": node.id,
                    "reason": issue.message,
                    "repair_hint": "",
                }
            )
    return issues


def _audit_issue_changes_plan(plan: QueryPlan, issue: dict[str, Any]) -> bool:
    changes = issue.get("set")
    if not isinstance(changes, dict) or not changes:
        return True
    return _apply_audit_patches(plan, {"issues": [issue]}) != plan


def _query_plan_compile_error(plan: QueryPlan) -> CapabilityIssue | None:
    for node in plan.nodes:
        if node.node_type != "query_data":
            continue
        if node.spec.subject not in {"disease", "patrol"}:
            continue
        try:
            sql = compile_spec_mdl(node.spec)
        except UnsupportedQuerySpec as exc:
            message = str(exc) or f"节点 {node.id} 无法编译"
            return _capability_issue_from_compile_error(
                node.id,
                message,
            )
        if not sql:
            return CapabilityIssue(
                code="empty_compilation",
                original_value=node.id,
                reason=f"节点 {node.id} 编译结果为空",
                node_id=node.id,
            )
    return None


def _node_outputs(spec: QuerySpec) -> set[str]:
    return {
        *spec.metrics,
        *spec.dimensions,
        *spec.output_projection,
    }


def _query_plan_derived_metric_issue(
    plan: QueryPlan,
    registry: CapabilityRegistry | None,
) -> CapabilityIssue | None:
    if registry is None:
        return None
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node in plan.nodes:
        if node.node_type != "post_calc":
            continue
        dependency_subjects = {
            nodes_by_id[dependency].spec.subject
            for dependency in node.depends_on
            if dependency in nodes_by_id
        }
        capability_subject = (
            next(iter(dependency_subjects))
            if len(dependency_subjects) == 1
            else node.spec.subject
        )
        allowed_atomic = tuple(
            registry.snapshot.metrics.get(capability_subject, {})
        )
        allowed_derived = tuple(
            registry.snapshot.derived_metrics.get(capability_subject, {})
        )
        declared_derived = any(
            registry.derived_metric(capability_subject, metric_id) is not None
            for metric_id in node.spec.metrics
        )
        if (
            "lower_is_better" in node.spec.required_assertions
            and allowed_derived
            and not declared_derived
        ):
            return CapabilityIssue(
                code="missing_derived_metric",
                original_value=node.id,
                reason=(
                    f"节点 {node.id} 声明了越低越好的评价，"
                    "但没有声明使用的派生指标"
                ),
                allowed_values=allowed_derived,
                suggested_repair=(
                    "从 allowed_values 中选择一个派生指标写入 post_calc.metrics，"
                    "并确保它的 operands 全部由依赖节点产出。"
                ),
                node_id=node.id,
                evidence={
                    "capability_subject": capability_subject,
                    "post_calc_subject": node.spec.subject,
                    "required_assertions": list(node.spec.required_assertions),
                    "available_derived_metrics": {
                        metric_id: {
                            "expression": metric.expression,
                            "operands": list(metric.operands),
                        }
                        for metric_id, metric in registry.snapshot.derived_metrics[
                            capability_subject
                        ].items()
                    },
                },
            )
        produced: set[str] = set()
        for dependency in node.depends_on:
            upstream = nodes_by_id.get(dependency)
            if upstream is not None:
                produced.update(_node_outputs(upstream.spec))
        for metric_id in node.spec.metrics:
            if registry.metric(capability_subject, metric_id) is not None:
                continue
            derived = registry.derived_metric(capability_subject, metric_id)
            if derived is None:
                return CapabilityIssue(
                    code="unknown_metric",
                    original_value=metric_id,
                    reason=(
                        f"节点 {node.id} 使用了未注册的指标 {metric_id}"
                    ),
                    allowed_values=allowed_atomic + allowed_derived,
                    suggested_repair=(
                        "只使用 allowed_values 中的指标；派生指标必须由 post_calc 计算。"
                    ),
                    node_id=node.id,
                )
            missing = tuple(
                operand for operand in derived.operands if operand not in produced
            )
            if missing:
                return CapabilityIssue(
                    code="missing_derived_operand",
                    original_value=metric_id,
                    reason=(
                        f"派生指标 {metric_id} 缺少上游操作数："
                        + "、".join(missing)
                    ),
                    allowed_values=missing,
                    suggested_repair=(
                        f"在依赖节点 {', '.join(node.depends_on) or '（未声明）'} "
                        f"的 metrics 中加入 {'、'.join(missing)}，"
                        f"再由 post_calc 计算 {derived.expression}。"
                    ),
                    node_id=node.id,
                    evidence={
                        "capability_subject": capability_subject,
                        "post_calc_subject": node.spec.subject,
                        "derived_metric": metric_id,
                        "operands": list(derived.operands),
                        "missing_operands": list(missing),
                        "dependency_node_ids": list(node.depends_on),
                        "produced_values": sorted(produced),
                        "expression": derived.expression,
                    },
                )
    return None


def _capability_issue_from_compile_error(
    node_id: str,
    message: str,
) -> CapabilityIssue:
    if "不支持指标：" in message:
        original = message.split("不支持指标：", 1)[1].split("；", 1)[0]
        allowed_text = message.split("可用指标：", 1)[1] if "可用指标：" in message else ""
        allowed = tuple(
            item.strip()
            for item in allowed_text.split("、")
            if item.strip()
        )
        return CapabilityIssue(
            code="unknown_metric",
            original_value=original,
            reason=message,
            allowed_values=allowed,
            suggested_repair=(
                "只使用 allowed_values 中的原子指标；派生指标改用 post_calc。"
            ),
            node_id=node_id,
        )
    return CapabilityIssue(
        code="unsupported_capability",
        original_value=node_id,
        reason=message,
        node_id=node_id,
    )


def _query_plan_compiles(plan: QueryPlan) -> bool:
    return _query_plan_compile_error(plan) is None


def _ambiguity_message(issues: list[Any]) -> str:
    reasons: list[str] = []
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or str(issue.get("code") or "") != "ambiguous_scope"
        ):
            continue
        reason = str(issue.get("reason") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return "；".join(reasons[:2])
    return "请补充具体时间范围、巡检批次、比较对象或所需字段。"


def analyze_single_turn(
    question: str,
    kind: str,
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
    *,
    audit: bool = False,
    registry: CapabilityRegistry | None = None,
    subject: str = "",
) -> QueryPlan:
    feedback = ""
    for attempt in range(3):
        try:
            plan = _analyze_once(
                question,
                kind,
                schema,
                reach,
                feedback,
                registry=registry,
                subject=subject,
            )
        except LLMError as exc:
            if attempt == 0:
                feedback = (
                    "上一轮模型没有返回合法 JSON。"
                    "请只输出符合 query_spec 格式的 JSON，不要附加解释。"
                    f"错误信息：{exc}"
                )
                continue
            raise
        except QuerySpecAmbiguity as exc:
            if attempt == 0:
                feedback = f"上一轮 QuerySpec 解析失败：{exc}"
                continue
            raise
        except QuerySpecError as exc:
            feedback = f"上一轮 QuerySpec 解析失败：{exc}"
            if attempt < 2:
                continue
            raise
        if not audit:
            return plan
        compile_issue = _query_plan_compile_error(plan)
        if compile_issue is None:
            compile_issue = _query_plan_derived_metric_issue(plan, registry)
        if compile_issue:
            feedback = json.dumps(
                compile_issue.to_feedback(),
                ensure_ascii=False,
                default=str,
            )
            if attempt < 2:
                continue
            if compile_issue.code == "missing_derived_operand":
                raise QuerySpecError(
                    "缺少派生指标操作数：" + compile_issue.reason
                )
            raise QuerySpecError(
                "当前数据能力不支持：" + compile_issue.reason
            )
        verdict = audit_query_plan(str(question or "").strip(), plan)
        if verdict.get("complete") is True:
            return plan
        post_calc_ids = {
            node.id
            for node in plan.nodes
            if node.node_type == "post_calc"
        }
        consumed_by_post_calc = {
            dependency
            for node in plan.nodes
            if node.node_type == "post_calc"
            for dependency in node.depends_on
        }
        effective_issues = []
        for issue in verdict.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            node_id = str(issue.get("node_id") or "")
            code = str(issue.get("code") or "")
            if node_id in post_calc_ids:
                continue
            if (
                node_id in consumed_by_post_calc
                and code
                in {
                    "missing_metric",
                    "wrong_grouping",
                    "missing_input_binding",
                    "unused_dependency",
                }
            ):
                if not (
                    code == "missing_metric"
                    and isinstance(issue.get("set"), dict)
                    and issue["set"]
                ):
                    continue
            if (
                not node_id
                and post_calc_ids
                and code in {"missing_metric", "wrong_grouping"}
            ):
                continue
            effective_issues.append(issue)
        effective_issues = [
            issue
            for issue in effective_issues
            if _audit_issue_changes_plan(plan, issue)
        ]
        if not effective_issues:
            return plan
        verdict = {**verdict, "issues": effective_issues}
        patched = _apply_audit_patches(plan, verdict)
        if patched != plan:
            contract_issues = _plan_contract_issues(patched, schema)
            if contract_issues:
                verdict = {
                    "complete": False,
                    "issues": contract_issues,
                }
            else:
                if _query_plan_compiles(plan) and not _query_plan_compiles(patched):
                    return plan
                repaired = audit_query_plan(str(question or "").strip(), patched)
                if repaired.get("complete") is True:
                    return patched
                verdict = repaired
        issues = verdict.get("issues") or []
        feedback = json.dumps(issues, ensure_ascii=False, default=str)
        if any(
            isinstance(item, dict)
            and str(item.get("code") or "") == "ambiguous_scope"
            for item in issues
        ):
            raise QuerySpecAmbiguity(_ambiguity_message(issues))
    raise QuerySpecAmbiguity(
        _ambiguity_message(issues)
    )
