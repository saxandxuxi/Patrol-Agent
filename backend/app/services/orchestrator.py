from __future__ import annotations

import re
from typing import Any

from . import prompts
from .logging_service import log_event
from .llm import LLMError, chat_json, role_model


def _llm_plan(message: str, kind: str) -> dict[str, Any]:
    # 拆分升级为"任务规划"（docs/项目智能化升级整体方案.md 升级1）：
    # 先想清楚"要完整回答这个问题需要哪几步、每步为了什么"，再产出子任务。
    # 老提示词只会按句子切，不会补"必须有一步综合分析"——
    # 所以"给个整体评价"这类问题常被拆成一堆数据查询，最后只把数字拼给用户。
    data = chat_json(
        prompts.messages("planning", question=message),
        # 规划决定整条链路的走向（做几步、要不要综合分析和查规范），是典型的语义理解任务
        model=role_model("semantic"),
    )
    raw_steps = data.get("steps", []) if isinstance(data, dict) else []
    if not isinstance(raw_steps, list):
        return {"is_simple": True, "final_goal": message, "steps": []}
    goal = str((data or {}).get("final_goal") or message)
    # action → 子任务类型（执行器只认 data / knowledge / analysis / post_calc）
    action_type = {
        "query_data": "data",
        "query_knowledge": "knowledge",
        "analyze": "analysis",
        # 看图 / 后置计算：由 LLM 判断，不再靠关键词表（"样图/两边加起来"这类说法列不完）
        "query_images": "image",
        "image": "image",
        "post_calc": "post_calc",
    }
    allowed_kinds = {"mysql", "postgresql", "seaweedfs", "knowledge"}

    def _kind(value: Any) -> str:
        picked = str(value or kind).strip().lower()
        return picked if picked in allowed_kinds else kind

    tasks: list[dict[str, Any]] = [
        {
            "type": action_type.get(str(step.get("action") or "query_data").strip(), "data"),
            "question": str(step.get("query") or "").strip(),
            "kind": _kind(step.get("kind") or step.get("datasource")),
            # 链式依赖：本步产出什么关键值 / 依赖前面哪一步的产出（见 docs/多步推理链式依赖整改方案.md）
            "produces": [
                item for item in (step.get("produces") or []) if isinstance(item, dict)
            ],
            "needs": [item for item in (step.get("needs") or []) if isinstance(item, dict)],
            # 图片是**某一步的属性**：这一步的结果要不要把图拿出来给人看（第一步要图/第二步不要图、
            # 反过来、都要图都要支持）。由语义规划给，不用关键词猜。
            "wants_images": bool(step.get("wants_images")),
        }
        for step in raw_steps
        if isinstance(step, dict)
    ]
    cleaned = []
    for index, task in enumerate(tasks, 1):
        question = str(task.get("question", "")).strip()
        task_type = str(task.get("type", "data")).strip()
        if not question:
            continue
        # post_calc：基于前序结果做算术/比较（不查库），由 agent 的后置计算器执行
        # analysis：综合分析（把前面 data/knowledge 的结果放一起推理），
        # 见 docs/长对话智能化升级完整方案-给codex.md 第二部分/第三部分。
        if task_type not in {"data", "aggregate", "knowledge", "post_calc", "analysis", "image"}:
            task_type = "data"
        # 规划器不给依赖关系，按语义补：**综合分析/后置计算**都依赖前面所有步骤
        # （后置计算要拿前面查到的数字去加/比/减，没有依赖就没有数据可算），
        # 其余步骤各自独立（顺序执行，后面的能看到前面已经查到的结果）。
        depends_on = (
            list(range(1, len(cleaned) + 1))
            if task_type in {"analysis", "post_calc"}
            else []
        )
        cleaned.append(
            {
                "id": index,
                "type": task_type,
                "question": question,
                "kind": task.get("kind") or kind,
                "depends_on": depends_on,
                # 链式依赖声明一路带到执行器，否则回填无从下手
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
    return {
        "is_simple": bool(data.get("is_simple")) if isinstance(data, dict) and "is_simple" in data else len(cleaned) <= 1,
        "final_goal": goal,
        "steps": cleaned,
    }


def _llm_tasks(message: str, kind: str) -> list[dict[str, Any]]:
    return list(_llm_plan(message, kind).get("steps") or [])


def decompose_plan(message: str, kind: str = "mysql") -> dict[str, Any]:
    """返回带目标、是否简单和步骤的规划草稿；供 ReAct 黑板使用。"""
    try:
        plan = _llm_plan(message, kind)
        if plan.get("steps"):
            return plan
    except (LLMError, Exception):
        pass
    return {
        "is_simple": True,
        "final_goal": message,
        "steps": [{"id": 1, "type": "data", "question": message}],
    }


def decompose(message: str, kind: str = "mysql") -> list[dict[str, Any]]:
    """任务拆分只由语义规划器决定；解析失败时按单任务处理。"""
    try:
        llm_tasks = _llm_tasks(message, kind)
        if llm_tasks:
            return llm_tasks
    except (LLMError, Exception):
        pass
    return [{"id": 1, "type": "data", "question": message}]
