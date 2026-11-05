from __future__ import annotations

from typing import Any

from . import orchestrator, prompts
from .llm import LLMError, chat_json, role_model
from .logging_service import log_error


_ALLOWED_DBS = ("mysql", "postgresql", "seaweedfs")


def _llm_classify_dbs(message: str, candidates: list[str]) -> list[str]:
    """数据源只由语义模型判断，代码只过滤模型返回的非法值。"""
    data = chat_json(
        prompts.messages(
            "datasource",
            candidates="、".join(candidates),
            message=message,
        ),
        model=role_model("semantic"),
    )
    datasources = [str(item).lower() for item in data.get("datasources", [])]
    return [item for item in datasources if item in candidates]


def db_scope(message: str) -> list[str]:
    """返回问题实际涉及的数据源；模型不可用时保守地只查 MySQL。"""
    try:
        resolved = _llm_classify_dbs(message, list(_ALLOWED_DBS))
    except (LLMError, Exception) as exc:  # noqa: BLE001
        log_error("cross_db", "datasource_classify_failed", exc)
        return ["mysql"]
    return resolved or ["mysql"]


def resolve_db_kind(message: str) -> str:
    """单库场景返回具体数据库；跨库场景由复合题执行器继续拆步骤。"""
    scope = db_scope(message)
    return scope[0] if len(scope) == 1 else "mysql"


def _task_kind(question: str, planned: Any, scope: list[str]) -> str:
    kind = str(planned or "").strip().lower()
    if kind in scope:
        return kind
    try:
        resolved = _llm_classify_dbs(question, scope)
    except (LLMError, Exception) as exc:  # noqa: BLE001
        log_error("cross_db", "task_datasource_classify_failed", exc, question=question[:80])
        resolved = []
    return resolved[0] if resolved else scope[0]


def decompose_cross_db(message: str, scope: list[str] | None = None) -> list[dict[str, Any]]:
    """整句交给语义规划器拆步骤，每个数据步骤再绑定一个数据源。"""
    scope = scope or db_scope(message)
    plan = orchestrator.decompose_plan(message, kind="")
    tasks = list(plan.get("steps") or [])
    if not tasks:
        tasks = [{"type": "data", "question": message}]
    out: list[dict[str, Any]] = []
    for task in tasks:
        task_type = str(task.get("type") or "data")
        item = dict(task)
        if task_type == "knowledge":
            item["kind"] = "knowledge"
        elif task_type in {"analysis", "post_calc"}:
            item["kind"] = ""
        else:
            item["kind"] = _task_kind(str(task.get("question") or message), task.get("kind"), scope)
        out.append(item)
    for index, task in enumerate(out, 1):
        task["id"] = index
    return out
