from __future__ import annotations

import json
import queue
import threading
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.responses import StreamingResponse

from ..services import nl2sql
from ..services import chat_settings
from ..services.agent import handle_message
from ..services.memory import clear_session
from ..config import settings
from ..store import (
    delete_favorite,
    list_favorites,
    save_favorite,
    save_user_feedback,
    all_conversation_ids,
    delete_conversation,
    delete_conversations,
    get_conversation_messages,
    get_result,
    list_conversations,
    list_history,
)


router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    # 复杂问题点了「确认查询」后前端回传 true，跳过复述确认直接查
    confirmed: bool = False
    # 「智能回答」旁路（docs/RAG示例直达与AI直答旁路方案.md §四）：本条消息跳过
    # 所有模板/示例候选卡，直接走完整 Wren 链路；只对本条生效，不改全局设置。
    force_ai: bool = False
    # 选择式澄清的回执：True=用已确认过的固化查询，False=让 AI 重新查，None=按复用策略走
    use_saved: bool | None = None
    # 能力确认卡的回执：choose_token + 选中的候选 id + 用户改过的槽位值
    choose_token: str | None = None
    capability_id: str | None = None
    slot_values: dict[str, str] | None = None


class ApproveRequest(BaseModel):
    result_id: str


class PurgeConversationsRequest(BaseModel):
    ids: list[str] = []
    all: bool = False


class ChatSettingsPayload(BaseModel):
    enable_thinking: bool | None = None
    enable_search: bool | None = None
    inherit_context: bool | None = None
    engine_mode: str | None = None
    # 能力复用策略：ask（命中先弹确认卡，默认）/ off（不推荐模板与示例）
    reuse_mode: str | None = None
    # L1 自动模板 / L2 示例直达的开关与门槛
    auto_template_enabled: bool | None = None
    # 自动挖掘（离线把成功 SQL 挖成模板）：修好挖掘器之前默认关，现在已修好、默认关的语义保留
    auto_template_mine_enabled: bool | None = None
    rag_direct_enabled: bool | None = None
    rag_direct_threshold: float | None = None
    rag_direct_margin: float | None = None
    rag_direct_topk: int | None = None
    rag_direct_fast_enabled: bool | None = None
    rag_direct_fast_threshold: float | None = None
    sql_model: str | None = None
    knowledge_model: str | None = None
    verify_model: str | None = None
    semantic_model: str | None = None
    planner_model: str | None = None
    controller_model: str | None = None



def require_token(
    request: Request, token: str | None = Header(default=None)
) -> None:
    """嵌入模式下的轻量鉴权（配了 WIDGET_TOKEN 才校验）：请求头 token 或 URL 参数 ?token=。"""
    # 支持多个口令（逗号分隔），方便给不同宿主/环境各发一个
    expected = [item.strip() for item in str(settings.widget_token or "").split(",") if item.strip()]
    if not expected:
        return
    given = str(token or request.query_params.get("token") or "").strip()
    if given not in expected:
        raise HTTPException(status_code=401, detail="token 无效或缺失")


@router.post("/chat", dependencies=[Depends(require_token)])
def chat_endpoint(payload: ChatRequest) -> dict:
    return handle_message(
        payload.message,
        payload.session_id,
        confirmed=payload.confirmed,
        use_saved=payload.use_saved,
        choose_token=payload.choose_token,
        capability_id=payload.capability_id,
        slot_values=payload.slot_values,
        force_ai=payload.force_ai,
    )


@router.post("/chat/stream", dependencies=[Depends(require_token)])
def chat_stream_endpoint(payload: ChatRequest) -> StreamingResponse:
    """以 SSE 推送过程事件；最终 payload 随 final 事件一起返回。"""
    events: queue.Queue[dict | None] = queue.Queue()

    def worker() -> None:
        try:
            handle_message(
                payload.message,
                payload.session_id,
                confirmed=payload.confirmed,
                use_saved=payload.use_saved,
                choose_token=payload.choose_token,
                capability_id=payload.capability_id,
                slot_values=payload.slot_values,
                force_ai=payload.force_ai,
                on_event=events.put,
            )
        except Exception as exc:  # noqa: BLE001
            # handle_message 正常会先发 error/done；这里兜住参数级异常。
            events.put(
                {
                    "type": "error",
                    "message": f"{type(exc).__name__}: {exc}"[:300],
                }
            )
        finally:
            events.put(None)

    threading.Thread(target=worker, name="chat-sse", daemon=True).start()

    def stream():
        yield ": connected\n\n"
        while True:
            event = events.get()
            if event is None:
                break
            yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/history")
def history_endpoint(limit: int = 20) -> list[dict]:
    return list_history(limit=limit)


@router.delete("/session/{session_id}")
def clear_session_endpoint(session_id: str) -> dict:
    """清掉指定会话的多轮上下文（前端点「新对话」时调用）。"""
    clear_session(session_id)
    return {"cleared": True, "session_id": session_id}


@router.get("/conversations")
def conversations(limit: int = 50) -> list[dict]:
    return list_conversations(limit=limit)


@router.get("/conversations/{conversation_id}")
def conversation_detail(conversation_id: str) -> dict:
    return {"id": conversation_id, "messages": get_conversation_messages(conversation_id)}


@router.get("/results/{result_id}")
def result_detail(result_id: str) -> dict:
    """取回某次查询的完整结果（表格/图表/SQL/血缘），用于回看历史。"""
    cached = get_result(result_id)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail=(
                "这次查询的结果已经被自动清理（本地只保留最近 "
                f"{settings.result_cache_limit} 条、{settings.result_cache_ttl_days} 天内的结果）。"
                "重新问一次就能看到同样的结果。"
            ),
        )
    return cached


@router.delete("/conversations/{conversation_id}")
def conversation_delete(conversation_id: str) -> dict:
    delete_conversation(conversation_id)
    clear_session(conversation_id)
    return {"deleted": True, "id": conversation_id}


@router.post("/conversations/purge")
def conversations_purge(payload: PurgeConversationsRequest) -> dict:
    """批量删除对话（一个个删太慢）：传 ids，或 all=true 清空全部。"""
    ids = [str(item) for item in payload.ids if item]
    if payload.all:
        ids = all_conversation_ids()
    if not ids:
        return {"deleted": 0, "ids": []}
    deleted = delete_conversations(ids)
    for conversation_id in ids:
        clear_session(conversation_id)  # 顺手清掉这些会话的多轮上下文
    return {"deleted": deleted, "ids": ids}


class FeedbackReport(BaseModel):
    question: str
    comment: str = ""
    sql: str = ""
    reply: str = ""
    kind: str = "bad"
    # 这次结果的 id：后端据此取出"用的哪条模板/示例 + 本轮条件"，自动补齐上下文
    result_id: str = ""


class QueryReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_token: str
    decision: Literal["accept", "reject"]


@router.post("/query/review", dependencies=[Depends(require_token)])
def query_review(payload: QueryReviewRequest) -> dict:
    from ..query.review import consume_review_gate, review_gate_error

    evidence = consume_review_gate(payload.review_token, payload.decision)
    if evidence is None:
        code = review_gate_error(payload.review_token)
        raise HTTPException(
            status_code=409,
            detail={
                "failure_code": code,
                "reason": (
                    "该裁决已经提交过"
                    if code == "review_token_consumed"
                    else "该裁决已失效，请重新提问"
                ),
            },
        )
    if payload.decision == "accept":
        sql = evidence.sections[0].sql if evidence.sections else ""
        save_user_feedback(
            evidence.original_question,
            sql_text=sql,
            reply="用户确认该结果回答了问题",
            comment="用户裁决：回答正确",
            kind="good",
            context=json.dumps(
                {
                    "review_decision": "accept",
                    "suspect_node_ids": list(evidence.suspect_node_ids),
                    "scope_hash": evidence.scope_hash,
                },
                ensure_ascii=False,
            ),
        )
        return {
            "accepted": True,
            "decision": "accept",
            "repair_required": False,
        }
    return {
        "accepted": False,
        "decision": "reject",
        "repair_required": True,
    }


@router.post("/feedback/report", dependencies=[Depends(require_token)])
def feedback_report(payload: FeedbackReport) -> dict:
    """用户反馈"答错了/答对了"。

    docs/模板沉淀标准与复用校验_系统性方案.md §5.2：**一键操作、不要求写原因**——
    上下文由后端自己收集（用哪条模板/示例、本轮条件、模板自带条件、差异），
    同一模板连续 3 次答错 → 自动降级回 candidate（只降不修，§八）。
    """
    context = _feedback_context(payload.result_id)
    template_id = str((context or {}).get("candidate_id") or "")
    save_user_feedback(
        payload.question,
        sql_text=payload.sql,
        reply=payload.reply,
        comment=payload.comment,
        kind=payload.kind,
        template_id=template_id,
        context=json.dumps(context or {}, ensure_ascii=False) if context else "",
    )
    demoted = False
    if payload.kind == "bad" and template_id:
        from ..store import recent_bad_feedback
        from ..services import templatizer

        recent = recent_bad_feedback(template_id, window=3)
        if len(recent) >= 3 and all(item == "bad" for item in recent):
            demoted = templatizer.demote(
                template_id, reason=f"连续 {len(recent)} 次被点答错（自动降级）"
            )
    return {"saved": True, "candidate_id": template_id, "demoted": demoted}


def _feedback_context(result_id: str) -> dict:
    """从缓存的结果里取出"这次用的是哪条候选 + 条件差异"，作为反馈的上下文。

    用户只需要点一下"答错了"，剩下的（模板/示例 id、本轮条件、模板自带条件、多带了哪些）
    全部由这里自动补齐——这是能规模化发现"看着对其实错"的关键。
    """
    name = str(result_id or "").strip()
    if not name:
        return {}
    from ..services import example_reuse, templatizer
    from ..store import get_result, list_recorded_example_pairs

    result = get_result(name) or {}
    candidate = result.get("candidate") or {}
    kind = str(candidate.get("kind") or "")
    cid = str(candidate.get("id") or "")
    plan_spec = result.get("spec") or {}
    plan_keys = sorted(example_reuse._reuse_condition_keys(plan_spec))  # noqa: SLF001
    detail: dict = {
        "engine": result.get("engine"),
        "candidate_kind": kind,
        "candidate_id": cid,
        "plan_conditions": plan_keys,
        "question": str(result.get("used_question") or "")[:120],
    }
    if kind in {"auto", "preset"} and cid:
        template = templatizer.find(cid) or {}
        match = template.get("match") or {}
        detail["template_conditions"] = list(match.get("conditions") or [])
        detail["template_fixed"] = dict(match.get("fixed") or {})
        detail["template_question"] = str(template.get("question") or "")[:120]
    elif kind in {"example", "example_direct", "saved", "saved_example"} and cid:
        row = next(
            (item for item in list_recorded_example_pairs(limit=2000) if str(item.get("doc_name")) == cid),
            None,
        )
        if row:
            try:
                spec = json.loads(str(row.get("spec_json") or "{}") or "{}")
            except (ValueError, TypeError):
                spec = {}
            cand_keys = sorted(example_reuse._reuse_condition_keys(spec))  # noqa: SLF001
            detail["example_conditions"] = cand_keys
            detail["extra_conditions"] = [key for key in cand_keys if key not in plan_keys]
            detail["missing_conditions"] = [key for key in plan_keys if key not in cand_keys]
            detail["example_question"] = str(row.get("question") or "")[:120]
    return detail


class FavoriteRequest(BaseModel):
    question: str


@router.get("/favorites", dependencies=[Depends(require_token)])
def favorites_list() -> dict:
    """查询收藏夹（方案五·3：用户自己维护的高频问法）。"""
    return {"items": list_favorites()}


@router.post("/favorites", dependencies=[Depends(require_token)])
def favorites_add(payload: FavoriteRequest) -> dict:
    return {"items": save_favorite(payload.question)}


@router.delete("/favorites", dependencies=[Depends(require_token)])
def favorites_delete(payload: FavoriteRequest) -> dict:
    return {"items": delete_favorite(payload.question)}


@router.get("/settings/chat")
def chat_settings_get() -> dict:
    return chat_settings.load()


@router.put("/settings/chat")
def chat_settings_put(payload: ChatSettingsPayload) -> dict:
    return chat_settings.save(payload.model_dump(exclude_none=True))


@router.post("/feedback/approve")
def approve_result(payload: ApproveRequest) -> dict:
    """用户确认结果正确后，把该问题-SQL 对存入示例库。"""
    cached = get_result(payload.result_id)
    if not cached:
        raise HTTPException(status_code=404, detail="结果已过期，无法确认。")
    # 结果缓存里存的是执行结果（含 used_question），payload 上的 question 不一定在缓存里
    question = cached.get("question") or cached.get("used_question") or ""
    sections = cached.get("sections") or []
    sql = cached.get("sql")
    if len(sections) == 1 and not sql:
        sql = sections[0].get("sql")
    has_section_sql = any(section.get("sql") for section in sections)
    if not question or (len(sections) > 1 and not has_section_sql) or (len(sections) <= 1 and not sql):
        raise HTTPException(status_code=400, detail="该结果没有可入库的 SQL。")

    note = ""
    used = cached.get("used_question")
    if used and used != question:
        note = f"用户补充后的问题：{used}"
    # 问法是否已经在示例库（本地/RAGFlow 问答集）里：是的话不再重复存，只标成已确认。
    # 和保存逻辑共用同一个判定（含"换个说法的同型变体"），接口才不会说反。
    existing = nl2sql.resolve_example_target(question, cached.get("spec"))

    # 复合问题走双层存储：外层存拆分结构、内层每个子问题各存一条 SQL
    # （见 docs/SQL问答对存储与检索方案.md §二 方案3）
    if len(sections) > 1:
        saved = nl2sql.save_composite_pair(
            question,
            sections,
            note=note,
            plan=cached.get("plan") or (cached.get("process") or {}).get("plan"),
        )
    else:
        # template=True → trusted=1：用户点"答对了/固化到看板"就是**人工确认**，
        # 这条要能进固定看板、也要能参与"同一句直接重放"
        # （见 docs/固定看板重构与收藏固化方案.md 第七章：approve 即默认收藏）。
        saved = nl2sql.save_successful_pair(
            question,
            sql,
            template=True,
            note=note,
            spec=cached.get("spec"),
            # 阶段4（结果解读）示例：把当时那句解读一起存下来
            # （见 docs/SQL问答对存储与检索方案.md §一 阶段4）
            interpretation=cached.get("reply") or "",
        )
    if not saved:
        raise HTTPException(status_code=502, detail="写入示例库失败，请检查 RAGFlow 连接。")
    # approve 即模板化（docs/RAG示例直达与AI直答旁路方案.md §2.5.2）：
    # 离线把这条成功 SQL 挖成参数化模板，**后台跑**，不占这次请求的时间。
    _templatize_in_background()
    return {
        "saved": True,
        "doc_name": (existing or {}).get("doc_name")
        or nl2sql.example_pair_name(question, sql or "composite"),
        "deduped": bool(existing),
        "note": note,
    }


def _templatize_in_background() -> None:
    """approve 之后异步补挖模板；失败只记日志，不影响接口返回。"""
    from ..services import templatizer

    templatizer.mine_in_background("templatize-after-approve")
