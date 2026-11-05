from __future__ import annotations

import json
import re
from typing import Any

from . import plan_contract, prompts
from .logging_service import log_event
from .llm import LLMError, chat_json, role_model


def _known_damage_types() -> list[str]:
    """规划前可用的病害类型白名单；读不到库时返回空列表，由规划契约保守处理。"""
    try:
        from . import nl2sql

        return list(nl2sql.known_problem_types("mysql"))
    except Exception:  # noqa: BLE001
        return []


def _dependency_id(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("from_step")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_needs(value: Any) -> tuple[list[dict[str, Any]], list[int]]:
    """把旧提示词的 `needs:[1]` 和结构化 needs 统一成一种依赖表示。"""
    needs: list[dict[str, Any]] = []
    dependency_ids: list[int] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            from_step = _dependency_id(item)
            normalized = dict(item)
            if from_step and not normalized.get("from_step"):
                normalized["from_step"] = from_step
            needs.append(normalized)
            if from_step:
                dependency_ids.append(from_step)
            continue
        from_step = _dependency_id(item)
        if from_step:
            needs.append({"from_step": from_step})
            dependency_ids.append(from_step)
    return needs, dependency_ids


def _should_execute_as_post_calc(question: str) -> bool:
    """行级遍历/分组取极值是计算事实，不能只交给 analyze 模型心算。"""
    text = str(question or "")
    if any(word in text for word in ("为什么", "建议", "原因", "判断依据", "综合说明")):
        return False
    return bool(
        re.search(
            r"(?:遍历|每(?:一)?(?:条|个|组)|组内).{0,30}(?:最多|最大|取第一条|Top)",
            text,
            re.I,
        )
    )


def _plan_from_model_data(
    data: dict[str, Any],
    message: str,
    kind: str,
    original_message: str,
    known_damage_types: list[str],
) -> dict[str, Any]:
    """把模型 JSON 收敛成执行器唯一认的计划结构，并挂上步骤契约。"""
    raw_steps = data.get("steps", []) if isinstance(data, dict) else []
    if not isinstance(raw_steps, list):
        return {"is_simple": True, "final_goal": message, "steps": []}
    goal = str((data or {}).get("final_goal") or message)
    action_type = {
        "query_data": "data",
        "query_knowledge": "knowledge",
        "analyze": "analysis",
        "query_images": "image",
        "image": "image",
        "post_calc": "post_calc",
    }
    allowed_kinds = {"mysql", "postgresql", "seaweedfs", "knowledge"}

    def _kind(value: Any) -> str:
        picked = str(value or kind).strip().lower()
        return picked if picked in allowed_kinds else kind

    tasks: list[dict[str, Any]] = []
    for step in raw_steps:
        if not isinstance(step, dict):
            continue
        needs, need_dependencies = _normalize_needs(step.get("needs"))
        explicit_dependencies = [
            item
            for item in (
                _dependency_id(dep)
                for dep in (
                    step.get("depends_on")
                    if isinstance(step.get("depends_on"), list)
                    else []
                )
            )
            if item
        ]
        tasks.append(
            {
                "type": action_type.get(
                    str(step.get("action") or "query_data").strip(), "data"
                ),
                "question": str(step.get("query") or "").strip(),
                "kind": _kind(step.get("kind") or step.get("datasource")),
                "produces": [
                    item
                    for item in (step.get("produces") or [])
                    if isinstance(item, dict)
                ],
                "needs": needs,
                "_declared_dependencies": list(
                    dict.fromkeys(explicit_dependencies + need_dependencies)
                ),
                "wants_images": bool(step.get("wants_images")),
            }
        )

    cleaned: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, 1):
        question = str(task.get("question", "")).strip()
        task_type = str(task.get("type", "data")).strip()
        if not question:
            continue
        if task_type not in {
            "data",
            "aggregate",
            "knowledge",
            "post_calc",
            "analysis",
            "image",
        }:
            task_type = "data"
        if task_type == "analysis" and _should_execute_as_post_calc(question):
            log_event(
                "orchestrator",
                "INFO",
                "analysis_promoted_to_post_calc",
                question=question[:100],
            )
            task_type = "post_calc"
        declared = [
            int(item)
            for item in task.get("_declared_dependencies") or []
            if 0 < int(item) < index
        ]
        depends_on = (
            list(range(1, len(cleaned) + 1))
            if task_type in {"analysis", "post_calc"} and not declared
            else declared
        )
        cleaned.append(
            {
                "id": index,
                "type": task_type,
                "question": question,
                "kind": task.get("kind") or kind,
                "depends_on": depends_on,
                "produces": task.get("produces") or [],
                "needs": task.get("needs") or [],
                "calc_logic": str(task.get("calc_logic") or ""),
                "wants_images": bool(task.get("wants_images")),
                "_plan_goal": goal,
            }
        )

    scalar_keys = {
        str(item.get("key") or "")
        for task in cleaned
        for item in task.get("produces") or []
        if isinstance(item, dict)
        and item.get("key")
        and (
            item.get("from_column")
            or re.match(
                r"^(?:top|peak|max|min|count|avg|sum|latest|earliest)_",
                str(item.get("key") or ""),
            )
        )
    }
    for task in cleaned:
        task["produces"] = [
            item
            for item in task.get("produces") or []
            if isinstance(item, dict) and str(item.get("key") or "") in scalar_keys
        ]
    for task in cleaned:
        if task.get("type") not in {"analysis", "post_calc"}:
            continue
        question = str(task.get("question") or "")

        def _replace_bundle(match: re.Match[str]) -> str:
            key = str(match.group(1))
            return match.group(0) if key in scalar_keys else "前序查询结果"

        fixed_question = re.sub(
            r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
            _replace_bundle,
            question,
        )
        if fixed_question != question:
            log_event(
                "orchestrator",
                "INFO",
                "bundle_placeholder_replaced",
                question=fixed_question[:80],
            )
            task["question"] = fixed_question
            task["needs"] = [
                item
                for item in task.get("needs") or []
                if str(item.get("key") or "") in scalar_keys
            ]

    issues: list[dict[str, Any]] = []
    for task in cleaned:
        contract = plan_contract.build_step_contract(
            original_message or message,
            task,
            known_damage_types=known_damage_types,
        )
        task["contract"] = contract
        if contract["question"] != task["question"]:
            log_event(
                "orchestrator",
                "INFO",
                "step_question_contract_normalized",
                before=str(task["question"])[:100],
                after=str(contract["question"])[:100],
            )
            task["question"] = contract["question"]
        for violation in contract.get("violations") or []:
            issues.append(
                {
                    "step_id": task["id"],
                    "code": violation["code"],
                    "message": violation["message"],
                }
            )
    return {
        "is_simple": (
            bool(data.get("is_simple"))
            if isinstance(data, dict) and "is_simple" in data
            else len(cleaned) <= 1
        ),
        "final_goal": goal,
        "steps": cleaned,
        "contract_issues": issues,
    }


def _repair_plan(
    message: str,
    kind: str,
    original_message: str,
    plan: dict[str, Any],
    issues: list[dict[str, Any]],
    known_damage_types: list[str],
) -> dict[str, Any] | None:
    if not issues:
        return None
    try:
        data = chat_json(
            prompts.messages(
                "repair_plan",
                question=original_message or message,
                planning_question=message,
                plan=json.dumps(plan, ensure_ascii=False, default=str),
                issues=json.dumps(issues, ensure_ascii=False, default=str),
                known_damage_types="、".join(known_damage_types) or "（字典暂不可用）",
            ),
            model=role_model("planner"),
        )
    except (LLMError, Exception):  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    repaired = _plan_from_model_data(
        data,
        message,
        kind,
        original_message,
        known_damage_types,
    )
    log_event(
        "orchestrator",
        "INFO",
        "plan_contract_repaired",
        before_steps=len(plan.get("steps") or []),
        after_steps=len(repaired.get("steps") or []),
        remaining_issues=len(repaired.get("contract_issues") or []),
    )
    return repaired


def _add_plan_level_contract_issues(
    original_message: str,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """校验跨步骤字段组合，避免同一分组视图被拆成不可关联的两张表。"""
    issues: list[dict[str, Any]] = []
    original = str(original_message or "")
    wants_route_group = bool(re.search(r"按路线|每条路线|各路线的", original))
    wants_route_length = bool(
        re.search(r"路线(?:的)?长度|长度[（(]?\s*km|route_length_km", original, re.I)
    )
    grouped_route_steps = [
        step
        for step in steps
        if str(step.get("type") or "") == "data"
        and str((step.get("contract") or {}).get("group_by") or "") == "route"
    ]
    if wants_route_group and wants_route_length:
        length_is_grouped = any(
            "length" in list((step.get("contract") or {}).get("metrics") or [])
            for step in grouped_route_steps
        )
        if not length_is_grouped:
            step_id = (
                int(grouped_route_steps[0].get("id") or 1)
                if grouped_route_steps
                else 1
            )
            issues.append(
                {
                    "step_id": step_id,
                    "code": "incompatible_route_length_view",
                    "message": (
                        "用户要求按路线分组并关联该路线长度，长度必须与路线指标同组返回；"
                        "不要把全库路线台账单独查成 500 行再由后置计算猜测关联。"
                    ),
                }
            )
    if re.search(r"更严重|最严重", original) and re.search(r"病害|泛油", original):
        available = {
            metric
            for step in grouped_route_steps
            for metric in (step.get("contract") or {}).get("metrics") or []
        }
        missing = [
            metric
            for metric in ("count", "total_area", "length")
            if metric not in available
        ]
        if missing:
            step_id = (
                int(grouped_route_steps[0].get("id") or 1)
                if grouped_route_steps
                else 1
            )
            issues.append(
                {
                    "step_id": step_id,
                    "code": "missing_severity_dimensions",
                    "message": (
                        "严重程度判断缺少同口径指标："
                        + "、".join(missing)
                        + "；按路线分组时需同时返回数量、总面积和长度，密度由后续计算得到。"
                    ),
                }
            )
        if not any(str(step.get("type") or "") == "analysis" for step in steps):
            issues.append(
                {
                    "step_id": (
                        int(grouped_route_steps[-1].get("id") or 1)
                        if grouped_route_steps
                        else 1
                    ),
                    "code": "missing_severity_analysis",
                    "message": (
                        "严重程度题必须有最后一步 analyze，综合数量、面积和密度"
                        "说明哪个维度决定结论。"
                    ),
                }
            )
    return issues


def _llm_plan(
    message: str,
    kind: str,
    original_message: str | None = None,
) -> dict[str, Any]:
    # 拆分升级为"任务规划"（docs/项目智能化升级整体方案.md 升级1）：
    # 先想清楚"要完整回答这个问题需要哪几步、每步为了什么"，再产出子任务。
    # 老提示词只会按句子切，不会补"必须有一步综合分析"——
    # 所以"给个整体评价"这类问题常被拆成一堆数据查询，最后只把数字拼给用户。
    known_damage_types = _known_damage_types()
    data = chat_json(
        prompts.messages(
            "planning",
            question=message,
            known_damage_types="、".join(known_damage_types) or "（字典暂不可用）",
        ),
        # 规划决定整条链路的走向（做几步、要不要综合分析和查规范），是典型的语义理解任务
        model=role_model("planner"),
    )
    if not isinstance(data, dict):
        return {"is_simple": True, "final_goal": message, "steps": []}
    plan = _plan_from_model_data(
        data,
        message,
        kind,
        original_message or message,
        known_damage_types,
    )
    def _attach(plan_data: dict[str, Any]) -> list[dict[str, Any]]:
        plan_issues = list(plan_data.get("contract_issues") or [])
        plan_issues.extend(
            _add_plan_level_contract_issues(
                original_message or message,
                list(plan_data.get("steps") or []),
            )
        )
        plan_data["contract_issues"] = plan_issues
        for issue in plan_issues:
            target = next(
                (
                    step
                    for step in plan_data.get("steps") or []
                    if int(step.get("id") or 0) == int(issue.get("step_id") or 0)
                ),
                None,
            )
            contract = (target or {}).get("contract")
            if not isinstance(contract, dict):
                continue
            existing_codes = {
                str(item.get("code") or "")
                for item in contract.get("violations") or []
                if isinstance(item, dict)
            }
            if str(issue.get("code") or "") not in existing_codes:
                contract["violations"].append(issue)
            contract["status"] = "invalid"
        return plan_issues

    plan_issues = _attach(plan)
    repaired = _repair_plan(
        message,
        kind,
        original_message or message,
        plan,
        plan_issues,
        known_damage_types,
    )
    if repaired:
        _attach(repaired)
        return repaired
    return plan


def _llm_tasks(
    message: str,
    kind: str,
    original_message: str | None = None,
) -> list[dict[str, Any]]:
    return list(_llm_plan(message, kind, original_message).get("steps") or [])


def decompose_plan(
    message: str,
    kind: str = "mysql",
    original_message: str | None = None,
) -> dict[str, Any]:
    """返回带目标、是否简单和步骤的规划草稿；供 ReAct 黑板使用。"""
    try:
        plan = _llm_plan(message, kind, original_message)
        if plan.get("steps"):
            return plan
    except (LLMError, Exception):
        pass
    return {
        "is_simple": True,
        "final_goal": message,
        "steps": [{"id": 1, "type": "data", "question": message}],
    }


def decompose(
    message: str,
    kind: str = "mysql",
    original_message: str | None = None,
) -> list[dict[str, Any]]:
    """任务拆分只由语义规划器决定；解析失败时按单任务处理。"""
    try:
        llm_tasks = _llm_tasks(message, kind, original_message)
        if llm_tasks:
            return llm_tasks
    except (LLMError, Exception):
        pass
    return [{"id": 1, "type": "data", "question": message}]
