from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from . import prompts, react_repair, tools
from . import event_bus
from .llm import chat_json, role_model
from .logging_service import log_error
from .tools.observation import normalize_week_label, observation
from ..store import get_result


@dataclass
class ReactBlackboard:
    full_question: str
    intent: str
    goal: str
    plan: dict[str, Any]
    spec: dict[str, Any] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    facts_cache: dict[str, Any] = field(default_factory=dict)
    used_plan_steps: set[int] = field(default_factory=set)
    max_steps: int = 5

    def add(self, step: dict[str, Any], observation_result: dict[str, Any]) -> None:
        self.observations.append(
            {
                "step": len(self.observations) + 1,
                "plan_step_id": int(step.get("id") or 0),
                "action": str(step.get("type") or ""),
                "question": str(step.get("question") or ""),
                "observation": observation_result,
            }
        )
        produced = (observation_result.get("data") or {}).get("produced") or {}
        if isinstance(produced, dict):
            self.values.update(
                {str(key): str(value) for key, value in produced.items() if value}
            )

    def prompt_state(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps_done": len(self.observations),
            "values": self.values,
            "facts": self.facts_cache,
        }


def should_use_react(message: str, tasks: list[dict[str, Any]]) -> bool:
    """多步可编排任务统一交给 ReAct；知识/数据/分析混合也适用。"""
    if len(tasks) < 2:
        return False
    types = {str(item.get("type") or "") for item in tasks}
    if not types <= {"data", "aggregate", "knowledge", "analysis", "post_calc"}:
        return False
    return True


def _need_keys(step: dict[str, Any]) -> list[str]:
    return [
        str(item.get("key") or "")
        for item in step.get("needs") or []
        if isinstance(item, dict) and item.get("key")
    ]


def _dependency_ids(step: dict[str, Any]) -> list[int]:
    ids = {int(item) for item in step.get("depends_on") or [] if str(item).isdigit()}
    ids.update(
        int(item.get("from_step"))
        for item in step.get("needs") or []
        if isinstance(item, dict) and item.get("from_step")
    )
    return sorted(ids)


def _has_dependent_steps(steps: list[dict[str, Any]], index: int) -> bool:
    """当前步骤被后续步骤依赖时，不能通过 skip_step 绕过失败。"""
    current = steps[index]
    current_id = int(current.get("id") or 0)
    produced_keys = {
        str(item.get("key") or "")
        for item in current.get("produces") or []
        if isinstance(item, dict) and item.get("key")
    }
    for later in steps[index + 1 :]:
        if current_id in {int(item) for item in later.get("depends_on") or []}:
            return True
        if any(
            str(item.get("from_step") or "") == str(current_id)
            or str(item.get("key") or "") in produced_keys
            for item in later.get("needs") or []
            if isinstance(item, dict)
        ):
            return True
        if any(
            f"{{{{{key}}}}}" in str(later.get("question") or "")
            for key in produced_keys
        ):
            return True
    return False


def _frozen_step(step: dict[str, Any], values: dict[str, str]) -> tuple[str, list[str]]:
    question = str(step.get("question") or "")
    missing: list[str] = []

    def replace(match: Any) -> str:
        key = str(match.group(1))
        value = str(values.get(key) or "")
        if not value:
            missing.append(key)
            return match.group(0)
        return value

    import re

    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", replace, question), missing


def _tool_for_step(step: dict[str, Any]) -> str:
    return {
        "data": "query_data",
        "aggregate": "query_data",
        "knowledge": "query_knowledge",
        "image": "fetch_images",
        "post_calc": "post_calc",
        "analysis": "analyze",
    }.get(str(step.get("type") or ""), "")


def _run_atomic_tool(
    name: str, frozen_query: str, values: dict[str, str]
) -> dict[str, Any] | None:
    """把控制器的原子工具别名落到真实注册工具；无法落地时返回 None。"""
    requested = str(name or "").strip()
    if not requested:
        return None
    if requested == "chain_value":
        actual, kwargs = "fill_placeholders", {"question": frozen_query, "values": values}
    elif requested == "time_convert":
        actual, kwargs = "parse_time_range", {"message": frozen_query}
    elif requested == "pile_convert":
        actual, kwargs = "parse_pile_range", {"message": frozen_query}
    elif requested == "enum_map":
        actual, kwargs = "resolve_damage_type", {"damage_text": frozen_query}
    else:
        return None
    try:
        tools.get_tool(actual)
    except KeyError:
        return None
    return tools.run_tool(actual, **kwargs)


def _failure_payload(
    blackboard: ReactBlackboard,
    sections: list[dict[str, Any]],
    step: dict[str, Any],
    obs: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    data = obs.get("data") or {}
    result_id = str(data.get("result_id") or "")
    cached = get_result(result_id) if result_id else None
    observed = str(
        data.get("answer")
        or (cached or {}).get("reply")
        or obs.get("summary")
        or obs.get("error")
        or ""
    ).strip()
    failure = obs.get("failure") or {}
    explained = str(decision.get("message") or decision.get("thought") or "").strip()
    reason = str(
        failure.get("reason")
        or obs.get("error")
        or explained
        or "执行失败"
    ).strip()
    reply = f"这一步没有继续重试：{reason}"
    if observed and observed not in reason:
        reply += f"\n\n已获得的部分结果：\n{observed}"
    payload = _payload(reply, blackboard, sections)
    payload.update(
        {
            "source": "error",
            "error": reason,
            "failed_step": {
                "id": step.get("id"),
                "tool": obs.get("tool"),
                "question": str(step.get("question") or ""),
                "summary": str(obs.get("summary") or ""),
                "hint": str(obs.get("next_hint") or ""),
            },
        }
    )
    return payload


def _gate_issue(step: dict[str, Any], obs: dict[str, Any]) -> str:
    if not obs.get("ok"):
        return "tool_error"
    data = obs.get("data") or {}
    if data.get("await_user"):
        return "ask_user"
    produced = data.get("produced") or {}
    missing = [
        item.get("key")
        for item in step.get("produces") or []
        if item.get("key") and not produced.get(item.get("key"))
    ]
    if missing:
        return "missing_produce:" + ",".join(str(item) for item in missing)
    return ""


def _tool_results(blackboard: ReactBlackboard) -> dict[int, dict[str, Any]]:
    """把已执行的 observation 还原成 analyze/post_calc 需要的真实前序结果。"""
    results: dict[int, dict[str, Any]] = {}
    for item in blackboard.observations:
        step_id = int(item.get("plan_step_id") or 0)
        if not step_id:
            continue
        obs = item.get("observation") or {}
        data = obs.get("data") or {}
        result_id = str(data.get("result_id") or "")
        cached = get_result(result_id) if result_id else None
        rows = list((cached or {}).get("rows") or data.get("preview_rows") or [])
        results[step_id] = {
            "question": str(item.get("question") or ""),
            "reply": str(data.get("answer") or obs.get("summary") or ""),
            "rows": rows,
            "columns": list((cached or {}).get("columns") or data.get("columns") or []),
            "source": (cached or {}).get("source") or data.get("source"),
            "engine": data.get("engine"),
            "produced": dict(data.get("produced") or {}),
            "_task_type": str(item.get("action") or ""),
        }
    return results


def _deviation_decision(
    blackboard: ReactBlackboard,
    step: dict[str, Any],
    frozen_query: str,
    obs: dict[str, Any],
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    repair_history: list[dict[str, Any]] | None = None,
    previous_calls: set[tuple[str, str]] | None = None,
    helper_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    failure = obs.get("failure") or {}
    allowed_tools = _allowed_tools(failure)
    payload = {
        "goal": blackboard.goal,
        "current_step": step,
        "frozen_query": frozen_query,
        "observation_error": obs,
        "failure": failure,
        "allowed_tools": allowed_tools,
        "previous_attempts": list(repair_history or []),
        "previous_calls": sorted(previous_calls or set()),
        "helper_observations": list(helper_observations or []),
        "coverage_and_values": {
            "coverage": obs.get("coverage") or {},
            "values": blackboard.values,
        },
    }
    if handler:
        return handler(payload)
    data = chat_json(
        prompts.messages(
            "react_controller",
            goal=blackboard.goal,
            current_step=json.dumps(step, ensure_ascii=False, default=str),
            frozen_query=frozen_query,
            observation_error=json.dumps(obs, ensure_ascii=False, default=str),
            failure=json.dumps(failure, ensure_ascii=False, default=str),
            allowed_tools=json.dumps(allowed_tools, ensure_ascii=False, default=str),
            previous_attempts=json.dumps(
                payload["previous_attempts"], ensure_ascii=False, default=str
            ),
            helper_observations=json.dumps(
                payload["helper_observations"], ensure_ascii=False, default=str
            ),
            coverage_and_values=json.dumps(
                payload["coverage_and_values"], ensure_ascii=False, default=str
            ),
        ),
        model=role_model("semantic"),
    )
    return data if isinstance(data, dict) else {}


def _allowed_tools(failure: dict[str, Any]) -> list[dict[str, Any]]:
    allowed: list[dict[str, Any]] = []
    for name in react_repair.candidate_tool_names(
        str(failure.get("failure_code") or "tool_error")
    ):
        try:
            item = tools.get_tool(name)
        except KeyError as exc:
            log_error("react", "repair_tool_missing", exc, tool=name)
            continue
        allowed.append(
            {
                "name": item.name,
                "layer": item.layer,
                "domain": item.domain,
                "description": item.description,
                "input_schema": item.input_schema,
            }
        )
    return allowed


def _validate_decision(
    decision: dict[str, Any],
    allowed: list[str],
    previous_calls: set[tuple[str, str]],
) -> str:
    action = str(decision.get("action") or "")
    if action not in {
        "call_tool",
        "retry_tool",
        "retry_atomic",
        "skip_step",
        "ask_user",
        "abort",
    }:
        return f"不支持的动作：{action or '空'}"
    if action in {"call_tool", "retry_tool"}:
        name = str(decision.get("tool") or "")
        if name not in allowed:
            return f"不允许调用工具：{name or '空'}"
        arguments = decision.get("arguments") or {}
        if not isinstance(arguments, dict):
            return "工具参数必须是对象"
        if action == "call_tool":
            try:
                schema = tools.get_tool(name).input_schema
            except KeyError:
                return f"工具不存在：{name}"
            missing = [
                key
                for key in schema.get("required") or []
                if key not in arguments
            ]
            if missing:
                return "缺少工具参数：" + "、".join(missing)
        signature = (
            name,
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
        )
        if signature in previous_calls:
            return "重复调用相同工具和参数"
    return ""


def _decision_signature(decision: dict[str, Any]) -> tuple[str, str]:
    return (
        str(decision.get("tool") or ""),
        json.dumps(
            decision.get("arguments") or {},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
    )


def _observation_fingerprint(obs: dict[str, Any]) -> str:
    data = obs.get("data") or {}
    failure = obs.get("failure") or {}
    return json.dumps(
        {
            "failure_code": failure.get("failure_code"),
            "sql": (failure.get("evidence") or {}).get("sql"),
            "columns": data.get("columns") or [],
            "rows": list(data.get("preview_rows") or [])[:10],
            "summary": obs.get("summary"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _observation_has_data(obs: dict[str, Any]) -> bool:
    data = obs.get("data") or {}
    return bool(
        int(data.get("row_count") or 0)
        or data.get("preview_rows")
        or data.get("answer")
    )


def _run_structured_repair(
    blackboard: ReactBlackboard,
    step: dict[str, Any],
    frozen_query: str,
    obs: dict[str, Any],
    args: dict[str, Any],
    controller: Callable[[dict[str, Any]], dict[str, Any]] | None,
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    helper_observations: list[dict[str, Any]] = []
    current_tool = _tool_for_step(step)
    while obs.get("failure"):
        if int(state["rounds"]) >= react_repair.GLOBAL_REPAIR_LIMIT:
            return obs, args, {}
        allowed = [
            str(item.get("name") or "")
            for item in _allowed_tools(obs["failure"])
        ]
        if not bool(obs["failure"].get("retryable")) or not allowed:
            return obs, args, {
                "action": "abort",
                "message": str(obs["failure"].get("reason") or "该失败不需要重跑工具"),
            }
        decision = _deviation_decision(
            blackboard,
            step,
            frozen_query,
            obs,
            handler=controller,
            repair_history=state["history"],
            previous_calls=state["calls"],
            helper_observations=helper_observations,
        )
        error = _validate_decision(decision, allowed, state["calls"])
        if error:
            return obs, args, {**decision, "message": error}
        action = str(decision.get("action") or "")
        if action not in {"call_tool", "retry_tool"}:
            return obs, args, decision
        name = str(decision.get("tool") or "")
        signature = _decision_signature(decision)
        previous_observation = {
            "summary": str(obs.get("summary") or ""),
            "failure": dict(obs.get("failure") or {}),
        }
        previous_fingerprint = _observation_fingerprint(obs)
        if action == "retry_tool":
            if name != current_tool:
                return obs, args, {
                    **decision,
                    "message": f"retry_tool 只能重试当前工具 {current_tool}",
                }
            next_args = dict(args)
            if name == "query_data":
                next_args["repair_feedback"] = obs["failure"]
            next_obs = tools.run_tool(name, **next_args)
        else:
            next_args = dict(decision.get("arguments") or {})
            next_obs = tools.run_tool(name, **next_args)
            helper_observations.append(next_obs)
        state["calls"].add(signature)
        state["rounds"] += 1
        failed = obs.get("failure") or {}
        state["history"].append(
            {
                "attempt": int(state["rounds"]),
                "failure_code": failed.get("failure_code"),
                "action": action,
                "tool": name,
                "accepted": next_obs.get("accepted"),
            }
        )
        if (
            action == "retry_tool"
            and next_obs.get("failure")
            and _observation_fingerprint(next_obs) == previous_fingerprint
        ):
            current = dict(next_obs.get("failure") or {})
            previous_summary = str(previous_observation.get("summary") or "")[:180]
            current_summary = str(next_obs.get("summary") or "")[:180]
            reason = (
                "连续两次结果相同且仍未通过验收，已停止重试；"
                "可能是生成器或验收器存在问题。"
                f"上次答案：{previous_summary or '（空）'}；"
                f"本次答案：{current_summary or '（空）'}；"
                f"上次原因：{previous_observation['failure'].get('reason') or '未知'}；"
                f"本次原因：{current.get('reason') or '未知'}"
            )
            if _observation_has_data(next_obs):
                warning = {
                    "reason": reason,
                    "previous_observation": previous_observation,
                    "current_observation": {
                        "summary": str(next_obs.get("summary") or ""),
                        "failure": dict(next_obs.get("failure") or {}),
                    },
                    "note": "查询仍有语义警告，但已有可用结果，继续后续工作流。",
                }
                accepted_obs = dict(next_obs)
                accepted_obs.update(
                    {
                        "ok": True,
                        "accepted": True,
                        "failure": None,
                        "verification_warning": warning,
                    }
                )
                state["history"][-1]["accepted"] = True
                return accepted_obs, next_args, {
                    "action": "accept",
                    "message": reason,
                }
            current["reason"] = reason
            evidence = dict(current.get("evidence") or {})
            evidence["previous_observation"] = previous_observation
            evidence["current_observation"] = {
                "summary": str(next_obs.get("summary") or ""),
                "failure": dict(next_obs.get("failure") or {}),
            }
            current["evidence"] = evidence
            stopped = dict(next_obs)
            stopped.update({"accepted": False, "ok": False, "failure": current})
            return stopped, next_args, {"action": "abort", "message": reason}
        if action == "retry_tool":
            obs, args = next_obs, next_args
    return obs, args, {}


def _section(step: dict[str, Any], obs: dict[str, Any]) -> dict[str, Any]:
    data = obs.get("data") or {}
    result_id = str(data.get("result_id") or "")
    cached = get_result(result_id) if result_id else None
    question = str(data.get("question") or step.get("question") or "")
    plan_step = {
        "id": int(step.get("id") or 0),
        "type": str(step.get("type") or "data"),
        "question": question,
        "needs": list(step.get("needs") or []),
        "produces": list(step.get("produces") or []),
        "wants_images": bool(step.get("wants_images")),
    }
    rows = list((cached or {}).get("rows") or data.get("preview_rows") or [])
    rows = [
        {key: normalize_week_label(value) for key, value in row.items()}
        if isinstance(row, dict)
        else row
        for row in rows
    ]
    type_map = {
        "data": "data",
        "aggregate": "aggregate",
        "knowledge": "knowledge",
        "image": "image",
        "post_calc": "post_calc",
        "analysis": "analysis",
    }
    return {
        "id": int(step.get("id") or 0),
        "type": type_map.get(str(step.get("type") or ""), "data"),
        "title": _tool_for_step(step),
        "question": question,
        "reply": normalize_week_label(
            str(data.get("answer") or obs.get("summary") or "")
        ),
        "source": (cached or {}).get("source") or data.get("source"),
        "sql": (cached or {}).get("sql"),
        "columns": list((cached or {}).get("columns") or data.get("columns") or []),
        "rows": rows,
        "row_count": int(data.get("row_count") or len(rows)),
        "chart_data": (cached or {}).get("chart_data"),
        "result_id": result_id or None,
        "shape": (cached or {}).get("shape"),
        "spec": (cached or {}).get("spec"),
        "answer_format": (cached or {}).get("answer_format"),
        "lineage": (cached or {}).get("lineage"),
        "references": (cached or {}).get("references") or [],
        "engine": data.get("engine")
        or (cached or {}).get("engine")
        or ("ragflow" if str(step.get("type") or "") == "knowledge" else None),
        "needs": plan_step["needs"],
        "produces": plan_step["produces"],
        "plan": plan_step,
        "risk_warnings": list((cached or {}).get("risk_warnings") or data.get("risk_warnings") or []),
        "verification_warning": obs.get("verification_warning"),
        "wants_images": bool(step.get("wants_images")),
    }


def _grounded_answer(sections: list[dict[str, Any]]) -> str:
    analyses = [
        str(section.get("reply") or "").strip()
        for section in sections
        if str(section.get("type") or "") == "analysis"
        and str(section.get("reply") or "").strip()
    ]
    if analyses:
        return analyses[-1]
    parts: list[str] = []
    for section in sections:
        if not section.get("row_count") and str(section.get("type") or "") not in {
            "analysis",
            "post_calc",
            "knowledge",
        }:
            continue
        text = (
            str(section.get("reply") or "")
            .split("\n\n统计口径", 1)[0]
            .strip()
            .rstrip("。；; ")
        )
        if text and text not in parts:
            parts.append(text)
    return ("；".join(parts) + "。") if parts else ""


def _payload(
    answer: str,
    blackboard: ReactBlackboard,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    risk_warnings: list[str] = []
    for section in sections:
        for warning in section.get("risk_warnings") or []:
            text = str(warning or "").strip()
            if text and text not in risk_warnings:
                risk_warnings.append(text)
    return {
        "reply": answer or "已完成查询。",
        "source": "database",
        "sections": sections,
        "references": [],
        "result_id": str(uuid.uuid4()),
        "answer_format": "mixed",
        "lineage": {"sections": [item.get("lineage") for item in sections]},
        "question": blackboard.full_question,
        "used_question": blackboard.full_question,
        "reviewable": True,
        "engine": "react",
        "risk_warnings": risk_warnings,
        "plan": blackboard.plan,
        "process": {
            "steps": [
                {
                    "id": item["id"],
                    "type": item["type"],
                    "label": "",
                    "engine": item.get("engine"),
                    "rows": int(item.get("row_count") or len(item.get("rows") or [])),
                    "images": 0,
                    "wants_images": bool(item.get("wants_images")),
                    "question": item.get("question"),
                    "tool": item.get("title"),
                }
                for item in sections
            ],
            "chain_values": dict(blackboard.values),
        },
    }


def _execute_step(
    blackboard: ReactBlackboard,
    step: dict[str, Any],
    repair_feedback: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    step_type = str(step.get("type") or "")
    question, missing = _frozen_step(step, blackboard.values)
    if missing and step_type not in {"analysis", "post_calc"}:
        obs = observation(
            "react",
            ok=False,
            summary="缺少链式依赖：" + "、".join(missing),
            error="missing_values",
            next_hint="选择 retry_atomic 修正依赖值。",
        )
        return obs, {}, question
    tool_name = _tool_for_step(step)
    if not tool_name:
        obs = observation(
            "react",
            ok=False,
            summary=f"计划步骤类型没有对应工具：{step.get('type')}",
            error="unsupported_step",
            next_hint="回退静态执行器。",
        )
        return obs, {}, question
    if tool_name == "query_data":
        args: dict[str, Any] = {
            "question": question,
            "intent": "data",
            "kind": "mysql",
            "wren_first": True,
            "produces": list(step.get("produces") or []),
            "repair_feedback": repair_feedback,
        }
    elif tool_name == "query_knowledge":
        args = {"question": question}
    elif tool_name == "fetch_images":
        args = {"question": question, "force_ai": True}
    elif tool_name in {"analyze", "post_calc"}:
        prior = sorted(_tool_results(blackboard))
        depends_on = _dependency_ids(step)
        if step_type == "analysis":
            depends_on = prior
        args = {
            "task": {**step, "question": question, "depends_on": depends_on},
            "results": _tool_results(blackboard),
        }
    else:
        args = {"question": question, "intent": step_type}
    obs = tools.run_tool(tool_name, **args)
    return obs, args, question


def run(
    question: str,
    plan: dict[str, Any],
    controller: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """按冻结计划确定性推进；只有门控异常才调用偏差控制器。"""
    steps = list(plan.get("steps") or [])
    if not steps:
        return None
    blackboard = ReactBlackboard(
        full_question=question,
        intent=str(plan.get("intent") or "data"),
        goal=str(plan.get("final_goal") or question),
        plan=plan,
        spec=dict(plan.get("spec") or {}),
    )
    sections: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    failed_step_ids: set[int] = set()
    index = 0
    retry_count = 0
    last_decision: dict[str, Any] = {}
    repair_state = {"rounds": 0, "history": [], "calls": set()}
    event_bus.emit_event(
        "phase",
        title=f"ReAct 按冻结计划执行（{len(steps)} 步）",
    )
    try:
        while index < len(steps) and len(blackboard.observations) < blackboard.max_steps:
            step = steps[index]
            blocked_by = [
                item for item in _dependency_ids(step) if item in failed_step_ids
            ]
            if blocked_by:
                reason = (
                    "前置步骤 "
                    + "、".join(str(item) for item in blocked_by)
                    + " 未完成，当前步骤未执行。"
                )
                failed_step_ids.add(int(step.get("id") or 0))
                failures.append(
                    {
                        "step_id": int(step.get("id") or 0),
                        "question": str(step.get("question") or ""),
                        "failure": react_repair.build_failure(
                            "missing_dependency",
                            stage="dependency",
                            reason=reason,
                            evidence={"blocked_by": blocked_by},
                        ),
                        "decision": {"action": "abort", "message": reason},
                    }
                )
                index += 1
                continue
            event_bus.emit_event(
                "phase",
                title=f"步骤 {index + 1}/{len(steps)}：{_tool_for_step(step)}",
            )
            need_keys = _need_keys(step)
            dependency_ids = _dependency_ids(step)
            event_bus.emit_event(
                "thought",
                text=(
                    (
                        "依赖步骤已就绪："
                        + "、".join(str(item) for item in dependency_ids)
                        + (
                            "；依赖值："
                            + "、".join(
                                f"{key}={blackboard.values.get(key)}"
                                for key in need_keys
                            )
                            if need_keys
                            else ""
                        )
                    )
                    if dependency_ids
                    else "该步骤无前置依赖，按冻结问法直接执行。"
                ),
            )
            missing_needs = any(not blackboard.values.get(key) for key in _need_keys(step))
            if missing_needs and str(step.get("type") or "") not in {"analysis", "post_calc"}:
                obs = observation(
                    "react",
                    ok=False,
                    summary="依赖未就绪，当前步骤没有执行。",
                    error="missing_values",
                    next_hint="选择 retry_atomic 修正依赖值。",
                )
                args: dict[str, Any] = {}
                frozen_query = str(step.get("question") or "")
            else:
                obs, args, frozen_query = _execute_step(blackboard, step)
            observed_step = {**step, "question": frozen_query}
            blackboard.add(observed_step, obs)
            issue = _gate_issue(step, obs)
            failure = obs.get("failure")
            if not issue and not failure:
                section = _section(observed_step, obs)
                section.update(
                    {
                        "accepted": True,
                        "failure": None,
                        "attempt": int(repair_state["rounds"]) + 1,
                    }
                )
                sections.append(section)
                blackboard.used_plan_steps.add(int(step.get("id") or 0))
                retry_count = 0
                index += 1
                continue
            if failure:
                repaired, args, decision = _run_structured_repair(
                    blackboard,
                    step,
                    frozen_query,
                    obs,
                    args,
                    controller,
                    repair_state,
                )
                if repaired.get("accepted"):
                    blackboard.add(observed_step, repaired)
                    section = _section(observed_step, repaired)
                    section.update(
                        {
                            "accepted": True,
                            "failure": None,
                            "attempt": int(repair_state["rounds"]) + 1,
                        }
                    )
                    sections.append(section)
                    blackboard.used_plan_steps.add(int(step.get("id") or 0))
                    index += 1
                    continue
                last_decision = decision
                failure_entry = {
                    "step_id": int(step.get("id") or 0),
                    "question": frozen_query,
                    "failure": repaired.get("failure"),
                    "decision": decision,
                }
                failures.append(failure_entry)
                failed_step_ids.add(int(step.get("id") or 0))
                blackboard.used_plan_steps.add(int(step.get("id") or 0))
                index += 1
                continue
            if retry_count >= 1:
                return _failure_payload(
                    blackboard, sections, step, obs, last_decision
                )
            decision = _deviation_decision(
                blackboard, step, frozen_query, obs, handler=controller
            )
            last_decision = decision
            action = str(decision.get("action") or "")
            event_bus.emit_event(
                "thought",
                text=str(decision.get("thought") or f"观察异常：{issue}"),
            )
            event_bus.emit_event(
                "action",
                layer="controller",
                tool=action or "unknown",
                sub_question=frozen_query,
                values=list((decision.get("fix_values") or {}).keys())
                if isinstance(decision.get("fix_values"), dict)
                else [],
            )
            if action == "skip_step":
                if _has_dependent_steps(steps, index):
                    return _failure_payload(
                        blackboard,
                        sections,
                        step,
                        obs,
                        {
                            **decision,
                            "message": (
                                "当前步骤被后续分析依赖，不能跳过；"
                                "没有可用数据时不生成虚假综合分析。"
                            ),
                        },
                    )
                blackboard.used_plan_steps.add(int(step.get("id") or 0))
                retry_count = 0
                index += 1
                continue
            if action == "ask_user":
                asked = tools.run_tool(
                    "ask_user",
                    reason=str(decision.get("message") or obs.get("summary") or issue),
                    kind="mysql",
                )
                blackboard.add({**step, "id": step.get("id"), "type": "ask_user"}, asked)
                sections.append(_section({**step, "type": "analysis"}, asked))
                return _payload(str(asked.get("summary") or ""), blackboard, sections)
            if action == "retry_atomic":
                fix_values = decision.get("fix_values") or {}
                if not isinstance(fix_values, dict):
                    return _failure_payload(
                        blackboard, sections, step, obs, decision
                    )
                atomic_result = _run_atomic_tool(
                    str(decision.get("atomic_tool") or ""),
                    frozen_query,
                    dict(blackboard.values),
                )
                if atomic_result is not None:
                    blackboard.facts_cache["last_atomic_tool"] = atomic_result
                before = dict(blackboard.values)
                blackboard.values.update(
                    {str(key): str(value) for key, value in fix_values.items() if value}
                )
                if blackboard.values == before:
                    return _failure_payload(
                        blackboard,
                        sections,
                        step,
                        obs,
                        {
                            **decision,
                            "message": (
                                "当前异常没有可改变的依赖值或原子工具结果，"
                                "继续重跑同一冻结查询只会重复失败。"
                            ),
                        },
                    )
                retry_count += 1
                continue
            if action == "abort":
                return _failure_payload(blackboard, sections, step, obs, decision)
            return None
        if index < len(steps):
            return {
                "reply": f"ReAct 达到执行步数上限，仍有 {len(steps) - index} 步未执行。",
                "source": "error",
                "error": "ReAct 执行步数达到上限",
                "sections": sections,
                "references": [],
                "result_id": str(uuid.uuid4()),
                "plan": plan,
                "partial": True,
                "repair_history": list(repair_state["history"]),
                "failures": failures,
            }
        event_bus.emit_event("phase", title="拼接真实工具观察")
        payload = _payload(_grounded_answer(sections), blackboard, sections)
        if failures:
            failure_reasons = [
                str((item.get("failure") or {}).get("reason") or "未知原因")
                for item in failures
            ]
            payload["reply"] = (
                "部分步骤未完成："
                + "；".join(failure_reasons)
                + "\n\n"
                + str(payload.get("reply") or "")
            )
            payload["error"] = "；".join(failure_reasons)
        payload.update(
            {
                "partial": bool(failures),
                "repair_history": list(repair_state["history"]),
                "failures": failures,
            }
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        log_error("react", "react_run_failed", exc, question=question[:120])
        return {
            "reply": f"ReAct 执行异常：{type(exc).__name__}: {exc}",
            "source": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "sections": sections,
            "references": [],
            "result_id": str(uuid.uuid4()),
            "plan": plan,
            "partial": True,
            "repair_history": list(repair_state["history"]),
            "failures": failures,
        }
