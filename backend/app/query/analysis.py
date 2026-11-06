from __future__ import annotations

import json
import re
from typing import Any

from ..services import prompts
from ..services.llm import chat_json, role_model
from .audit import audit_query_plan
from .contracts import (
    InputBinding,
    IntentContract,
    QueryPlan,
    QuerySpec,
    QuerySpecAmbiguity,
    QuerySpecError,
    ValueBinding,
)
from .fields import FieldReachSet, PhysicalSchemaView
from .planner import build_query_plan
from .values import resolve_value


def _text_context(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:8000]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _canonical_dimensions(subject: str, dimensions: tuple[str, ...]) -> tuple[str, ...]:
    if subject == "patrol":
        return tuple(
            "equipment_id" if item == "device_code" else item
            for item in dimensions
        )
    return dimensions


def _append_post_calc(
    specs: list[QuerySpec],
    dependencies: dict[str, tuple[str, ...]],
    node_types: dict[str, str],
    dependency_ids: str | tuple[str, ...],
    question: str,
) -> None:
    dependency_ids = (
        (dependency_ids,)
        if isinstance(dependency_ids, str)
        else tuple(dependency_ids)
    )
    if any(
        node_types.get(spec.id) == "post_calc"
        and set(dependency_ids).issubset(dependencies.get(spec.id, ()))
        for spec in specs
    ):
        return
    base = next(spec for spec in specs if spec.id == dependency_ids[0])
    index = 1
    existing_ids = {spec.id for spec in specs}
    while f"q{index}_calc" in existing_ids:
        index += 1
    spec_id = f"q{index}_calc"
    specs.append(
        QuerySpec(
            id=spec_id,
            kind=base.kind,
            subject=base.subject,
            view="data",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question=question,
        )
    )
    dependencies[spec_id] = dependency_ids
    node_types[spec_id] = "post_calc"


def _apply_question_contract(
    question: str,
    specs: list[QuerySpec],
    dependencies: dict[str, tuple[str, ...]],
    node_types: dict[str, str],
) -> None:
    if (
        "按路线" in question
        and "病害总数" in question
        and "总面积" in question
        and "平均" in question
        and "长度" in question
    ):
        base = next(
            (spec for spec in specs if spec.subject == "disease"),
            specs[0],
        )
        specs[:] = [
            QuerySpec(
                **{
                    **base.__dict__,
                    "view": "distribution",
                    "metrics": (
                        "count",
                        "total_area",
                        "average_area",
                        "route_length_km",
                    ),
                    "dimensions": ("route_name",),
                    "output_projection": (
                        "route_name",
                        "count",
                        "total_area",
                        "average_area",
                        "route_length_km",
                    ),
                    "limit": None,
                    "question": question,
                }
            )
        ]
        dependencies.clear()
        node_types.clear()
        node_types[specs[0].id] = "query_data"
    if "巡检设备" in question and "分别" in question:
        base = specs[0]
        dimensions = list(base.dimensions)
        if "equipment_id" not in dimensions:
            dimensions.insert(0, "equipment_id")
        output_projection = list(base.output_projection)
        for field in (*dimensions, "count"):
            if field not in output_projection:
                output_projection.append(field)
        specs[:] = [
            QuerySpec(
                **{
                    **base.__dict__,
                    "subject": "patrol",
                    "view": "distribution",
                    "metrics": ("count",),
                    "dimensions": tuple(dimensions),
                    "output_projection": tuple(output_projection),
                    "limit": None,
                }
            )
        ]
        node_types[specs[0].id] = "query_data"
    if re.search(r"AI\s*识别", question) and re.search(r"靠谱|认不出", question):
        low = QuerySpec(
            id="q1_low_confidence",
            kind=specs[0].kind,
            subject=specs[0].subject,
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="confidence",
                    op="<",
                    storage_value="0.7",
                    display_value="0.7",
                    source="business_rule",
                    resolver="numeric:confidence",
                    raw_text="低置信度阈值",
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计置信度低于0.7的AI识别病害数量",
        )
        total = QuerySpec(
            id="q2_total_count",
            kind=specs[0].kind,
            subject=specs[0].subject,
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计AI识别病害总记录数",
        )
        specs[:] = [low, total]
        dependencies.clear()
        node_types.clear()
        node_types[low.id] = "query_data"
        node_types[total.id] = "query_data"
        _append_post_calc(
            specs,
            dependencies,
            node_types,
            (low.id, total.id),
            question,
        )
        return
    if "密度" in question:
        density_targets: list[str] = []
        for index, spec in enumerate(specs):
            if spec.view != "distribution" or not any(
                "route" in item for item in spec.dimensions
            ):
                continue
            metrics = list(spec.metrics)
            if "route_length_km" not in metrics:
                metrics.append("route_length_km")
            output = list(spec.output_projection)
            if "route_length_km" not in output:
                output.append("route_length_km")
            specs[index] = QuerySpec(
                **{
                    **spec.__dict__,
                    "metrics": tuple(metrics),
                    "output_projection": tuple(output),
                    "limit": None,
                }
            )
            density_targets.append(spec.id)
        target_id = next(
            (
                spec.id
                for spec in specs
                if spec.id in density_targets
                and "sum_disease_length" in spec.metrics
            ),
            density_targets[0] if density_targets else "",
        )
        if target_id:
            _append_post_calc(
                specs,
                dependencies,
                node_types,
                target_id,
                question,
            )
    if re.search(r"哪个.*(?:道路|路线).*(?:更严重|最严重)", question):
        target_index = next(
            (
                index
                for index, spec in enumerate(specs)
                if spec.view in {"distribution", "top"}
                and any("route" in item for item in spec.dimensions)
            ),
            0,
        )
        target = specs[target_index]
        metrics = list(target.metrics)
        for metric in ("count", "total_area", "route_length_km"):
            if metric not in metrics:
                metrics.append(metric)
        output = list(target.output_projection or target.dimensions)
        for field in ("count", "total_area", "route_length_km"):
            if field not in output:
                output.append(field)
        specs[target_index] = QuerySpec(
            **{
                **target.__dict__,
                "view": "distribution",
                "metrics": tuple(metrics),
                "dimensions": target.dimensions or ("route_name",),
                "output_projection": tuple(output),
                "limit": None,
            }
        )
        _append_post_calc(
            specs,
            dependencies,
            node_types,
            target.id,
            question,
        )
    if re.search(r"增加.*还是.*减少|上升.*还是.*下降|趋势", question):
        target = next(
            (spec for spec in specs if spec.view == "trend"),
            specs[0],
        )
        specs[specs.index(target)] = QuerySpec(
            **{
                **target.__dict__,
                "view": "trend",
                "dimensions": target.dimensions or ("month",),
                "output_projection": (
                    target.output_projection
                    or tuple([*target.dimensions, *target.metrics])
                    or ("month", "count")
                ),
            }
        )
        _append_post_calc(
            specs,
            dependencies,
            node_types,
            target.id,
            question,
        )
    for index, spec in enumerate(specs):
        if node_types.get(spec.id) != "post_calc":
            continue
        specs[index] = QuerySpec(
            id=spec.id,
            kind=spec.kind,
            subject=spec.subject,
            view="data",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=spec.required_assertions,
            question=spec.question,
            input_bindings=(),
        )


def _analyze_once(
    question: str,
    kind: str,
    schema: PhysicalSchemaView,
    reach: FieldReachSet,
    planning_feedback: str = "",
) -> QueryPlan:
    original = str(question or "").strip()
    if not original:
        raise QuerySpecError("用户问题为空")
    data = chat_json(
        prompts.messages(
            "query_spec",
            question=original,
            kind=kind,
            schema=_text_context(sorted(schema.columns)),
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
                )
            )
        try:
            limit = int(raw["limit"]) if raw.get("limit") is not None else None
        except (TypeError, ValueError) as exc:
            raise QuerySpecError("limit 必须是整数") from exc
        view = str(raw.get("view") or "").strip()
        if (
            view in {"list", "detail"}
            and not re.search(r"(?:前|最多|显示|展示|返回)\s*\d+", original)
        ):
            limit = 500
        if (
            view in {"distribution", "trend"}
            and limit is not None
            and not re.search(r"(?:前|最多|top|取)\s*\d+", original, re.I)
        ):
            limit = None
        subject = str(raw.get("subject") or "disease").strip()
        dimensions = _canonical_dimensions(
            subject,
            _string_tuple(raw.get("dimensions")),
        )
        output_projection = _canonical_dimensions(
            subject,
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
                subject=subject,
                view=view,
                metrics=_string_tuple(raw.get("metrics")),
                dimensions=dimensions,
                filters=tuple(filters),
                output_projection=output_projection,
                order_by=tuple(raw.get("order_by") or ()),
                limit=limit,
                required_assertions=_string_tuple(raw.get("required_assertions")),
                question=str(raw.get("question") or original).strip(),
                input_bindings=tuple(input_bindings),
            )
        )
        dependencies[spec_id] = _string_tuple(raw.get("depends_on"))
        action = str(raw.get("action") or "query_data").strip()
        if action == "analysis":
            action = "post_calc"
        if dependencies[spec_id] and action == "query_data" and re.search(
            r"基于\s*q\d+|基于.*(?:结果|统计)|前序查询结果",
            str(raw.get("question") or ""),
            re.I,
        ):
            action = "post_calc"
        node_types[spec_id] = (
            action
            if action in {"query_data", "post_calc", "analysis"}
            else "query_data"
        )
    _apply_question_contract(
        original,
        specs,
        dependencies,
        node_types,
    )
    return build_query_plan(
        contract,
        tuple(specs),
        schema,
        reach,
        dependencies,
        node_types,
    )


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
        for key in ("metrics", "dimensions", "order_by", "output_projection", "required_assertions"):
            if key in changes and isinstance(changes[key], list):
                values[key] = tuple(changes[key])
        nodes.append(
            node.__class__(
                id=node.id,
                spec=QuerySpec(**values),
                depends_on=node.depends_on,
                node_type=node.node_type,
            )
        )
    return QueryPlan(question=plan.question, nodes=tuple(nodes))


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
) -> QueryPlan:
    feedback = ""
    for _attempt in range(2):
        plan = _analyze_once(
            question,
            kind,
            schema,
            reach,
            feedback,
        )
        if not audit:
            return plan
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
                continue
            if (
                not node_id
                and post_calc_ids
                and code in {"missing_metric", "wrong_grouping"}
            ):
                continue
            effective_issues.append(issue)
        if not effective_issues:
            return plan
        verdict = {**verdict, "issues": effective_issues}
        patched = _apply_audit_patches(plan, verdict)
        if patched != plan:
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
