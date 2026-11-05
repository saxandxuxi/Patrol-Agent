"""固定看板：卡片 = 示例库里"答对了"固化的题（+ 系统无参模板）。

见 docs/固定看板重构与收藏固化方案.md 第一、二、七章。卡片点"刷新"时**不生成 SQL**：
直接重跑当初用户确认过的 SQL，只做执行 + 结果组装（analysis 类才多一次分析调用）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..config import settings
from ..store import (
    get_example_by_doc_name,
    list_board_examples,
    update_board_card,
)
from ..services import analysis as analysis_service
from ..services import sql_engine
from ..services.logging_service import log_error, log_event

router = APIRouter(prefix="/api")


def require_token(request: Request, token: str | None = Header(default=None)) -> None:
    expected = [item.strip() for item in str(settings.widget_token or "").split(",") if item.strip()]
    if not expected:
        return
    given = str(token or request.query_params.get("token") or "").strip()
    if given not in expected:
        raise HTTPException(status_code=401, detail="token 无效或缺失")


def _kind_of(sql: str) -> str:
    """这条固化 SQL 是哪边的库：命中 PG 白名单表就走 postgresql。"""
    lowered = (sql or "").lower()
    return "postgresql" if any(table in lowered for table in settings.pg_allowed_tables) else "mysql"


def _intent_of(spec: dict[str, Any] | None) -> str:
    spec = spec or {}
    shape = str(spec.get("shape") or "")
    if shape in {"distribution", "trend", "bucket_compare"}:
        return "analysis"
    if shape in {"count", "sum", "avg"}:
        return "data"
    return "list"


def _summarize(title: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "未查询到符合条件的数据"
    if len(rows) == 1 and len(columns) == 1:
        return f"{title}：{rows[0].get(columns[0])}"
    if len(rows) == 1 and 1 < len(columns) <= 4:
        row = rows[0]
        parts = [f"{column} {row.get(column)}" for column in columns if row.get(column) is not None]
        return "、".join(parts) or title
    if len(columns) >= 2 and len(rows) <= 6:
        head = rows[0]
        return f"{title}（共 {len(rows)} 行）：" + "、".join(
            f"{head.get(columns[0])} {head.get(columns[1])}" for _ in (0,)
        )
    return f"{title}（共 {len(rows)} 行，见表格）"


@router.get("/dashboard/cards", dependencies=[Depends(require_token)])
def dashboard_cards() -> dict:
    """看板卡片：示例库（RAGFlow 问答集 + 我固化的）里的问答对，加无参预置模板。

    不区分"系统 / 我的"：示例库里已经有 SQL 的问答对一律成为卡片。
    """
    from . import preset as preset_router

    # RAGFlow 里新加的问答对，先补进本地示例表（幂等、最多 60 秒一次）
    try:
        from ..services import nl2sql

        nl2sql.import_ragflow_examples()
    except Exception as exc:  # noqa: BLE001
        log_error("dashboard", "example_import_failed", exc)
    cards: list[dict[str, Any]] = []
    for row in list_board_examples():
        spec: dict[str, Any] = {}
        raw_spec = row.get("spec_json")
        if raw_spec:
            try:
                spec = json.loads(raw_spec) or {}
            except Exception:  # noqa: BLE001
                spec = {}
        cards.append(
            {
                "doc_name": row.get("doc_name"),
                # 展示用**对外问法**（长对话补全留下的"377 处"这类在入库时已清成 display_question）
                "question": spec.get("display_question") or row.get("question"),
                "title": spec.get("display_question") or row.get("question"),
                "intent": _intent_of(spec),
                "group_name": row.get("group_name") or "默认",
                "pinned": bool(row.get("pinned")),
                "on_board": bool(row.get("board")),
                "trusted": bool(row.get("trusted")),
                "last_summary": row.get("last_summary") or "",
                "last_run_at": row.get("last_run_at") or "",
                "created_at": row.get("created_at") or "",
                "scope": str(spec.get("scope") or ""),
                "variants": [
                    str(item) for item in (spec.get("variants") or []) if str(item).strip()
                ][:8],
                "params": [],
                "source": "example",
                "origin": "example",
                # 复合题外层：没有单条 SQL，重放时按子问题逐段跑（下面 run 里处理）
                "composite": bool(spec.get("subtasks_summary")),
            }
        )
    data = preset_router._load()
    for item in data.get("queries") or []:
        if item.get("params"):
            continue  # 带参数的模板归快捷查询，卡片只放"点一下就有数"的
        cards.append(
            {
                "doc_name": f"template:{item.get('id')}",
                "question": item.get("question"),
                "title": item.get("title"),
                "intent": "data",
                "group_name": item.get("category") or "系统模板",
                "pinned": False,
                "on_board": True,
                "last_summary": "",
                "last_run_at": "",
                "created_at": "",
                "scope": item.get("scope") or "",
                "params": [],
                "source": "template",
                "origin": "template",
                "preset_id": item.get("id"),
            }
        )
    # total：管理页/冒烟脚本要显示"共 N 张卡"，前端自己数当然也行，但汇总字段在服务端给出来
    # 更好——卡片还会按分组/来源聚合，客户端算容易和这里不一致
    # （backend/_codex_http_battery.py 就是读 total 的）
    return {
        "cards": cards,
        "total": len(cards),
        "counts": {
            "example": sum(1 for item in cards if str(item.get("origin")) == "example"),
            "template": sum(1 for item in cards if str(item.get("origin")) == "template"),
        },
    }


class RunCardRequest(BaseModel):
    doc_name: str


def _run_composite(doc_name: str, title: str, spec: dict[str, Any]) -> dict:
    """复合题重放：子问题都在本地示例库里，逐段执行 + 拼成 sections。"""
    from ..store import list_recorded_example_pairs

    examples = {
        str(row.get("question") or ""): str(row.get("sql_text") or "")
        for row in list_recorded_example_pairs(limit=2000)
        if str(row.get("sql_text") or "").strip()
    }
    sections: list[dict[str, Any]] = []
    parts: list[str] = []
    for item in spec.get("subtasks_summary") or []:
        question = str(item.get("question") or "")
        # 复合题外层优先用固化时随子任务保存的 SQL。内层示例可能因"同型问法"去重
        # 落到另一条问法下，这时再按当前 question 反查会误判"没有固化 SQL"。
        sql = str(item.get("sql") or examples.get(question) or "")
        if not sql:
            sections.append({"id": item.get("id"), "type": "missing", "question": question,
                             "reply": "这一步还没有固化 SQL，去对话里问一次并固化。", "rows": [], "columns": []})
            continue
        try:
            result = sql_engine.execute_sql(_kind_of(sql), sql)
        except Exception as exc:  # noqa: BLE001
            sections.append({"id": item.get("id"), "type": "error", "question": question,
                             "reply": f"这一步重跑失败：{exc}", "rows": [], "columns": []})
            continue
        columns = result.get("columns") or []
        rows = result.get("rows") or []
        summary = _summarize(question, columns, rows)
        parts.append(summary)
        sections.append(
            {
                "id": item.get("id"),
                "type": item.get("type") or "data",
                "question": question,
                "reply": summary,
                "sql": result.get("sql"),
                "columns": columns,
                "rows": rows,
                "chart_data": analysis_service.analyze(result, "data", "distribution"),
            }
        )
    update_board_card(doc_name, last_summary="；".join(parts)[:160], touch_run=True)
    log_event("dashboard", "INFO", "run_composite_ok", doc_name=doc_name, sections=len(sections))
    return {
        "reply": "\n".join(parts) or "这条复合题还没有可重放的子步骤。",
        "source": "saved_query",
        "engine": "saved",
        "intent": "composite",
        "sql": None,
        "columns": [],
        "rows": [],
        "chart_data": None,
        "sections": sections,
        "summary": "；".join(parts)[:160],
        "doc_name": doc_name,
        "scope": "",
    }


@router.post("/dashboard/run", dependencies=[Depends(require_token)])
def dashboard_run(payload: RunCardRequest) -> dict:
    """刷新一张卡片：重跑固化 SQL（或模板），不生成 SQL。"""
    doc_name = str(payload.doc_name or "")
    if doc_name.startswith("template:"):
        from . import preset as preset_router

        preset_id = doc_name.split(":", 1)[1]
        query = preset_router._query_by_id(preset_id)
        sql = preset_router._bind(query, {})
        title = str(query.get("title") or preset_id)
        scope = str(query.get("scope") or "")
    else:
        row = get_example_by_doc_name(doc_name)
        if not row:
            raise HTTPException(status_code=404, detail="这张卡片对应的固化查询不存在。")
        sql = str(row.get("sql_text") or "")
        scope = ""
        spec: dict[str, Any] = {}
        if row.get("spec_json"):
            try:
                spec = json.loads(str(row["spec_json"])) or {}
            except Exception:  # noqa: BLE001
                spec = {}
        title = str(spec.get("display_question") or row.get("question") or doc_name)
        if not sql.strip() and spec.get("subtasks_summary"):
            # 复合题：外层没 SQL，按子问题逐段重放（子问题各自是本地示例里的一条）
            return _run_composite(doc_name, title, spec)
        if re.search(r"\{[a-z_]+\}", sql):
            # 归一化 SQL（RAGFlow 导入的）：用它自己的 spec 把具体值填回去
            from ..services import nl2sql

            filled = nl2sql.fill_example_slots(sql, spec)
            if not filled:
                raise HTTPException(
                    status_code=400,
                    detail="这条示例的条件没法自动补全（缺路线/类型等取值），去对话里问一次再固化。",
                )
            sql = filled
    if not sql.strip():
        raise HTTPException(status_code=400, detail="这张卡片没有可重放的 SQL。")
    kind = _kind_of(sql)
    try:
        result = sql_engine.execute_sql(kind, sql)
    except Exception as exc:  # noqa: BLE001
        log_error("dashboard", "run_failed", exc, doc_name=doc_name)
        raise HTTPException(status_code=400, detail=f"重跑失败：{exc}") from exc
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    summary = _summarize(title, columns, rows)
    reply = f"{summary}\n\n统计口径：\n- {scope or '按你当初确认过的那版口径'}"
    chart = analysis_service.analyze(result, "analysis" if len(columns) > 2 else "data", "distribution")
    if not doc_name.startswith("template:"):
        update_board_card(doc_name, last_summary=summary, touch_run=True)
    log_event("dashboard", "INFO", "run_ok", doc_name=doc_name, rows=len(rows))
    # 图片类卡片（当初固化时的问法就是要看图）刷新时也要出图：拿问法再走一遍图片渲染
    # （哪几列算图片列来自字段目录 role=image；结果里一张图片路径都没有时这一步是空转，不发请求）
    payload: dict[str, Any] = {
        "reply": reply,
        "source": "saved_query",
        "engine": "saved",
        "intent": "data",
        "sql": result.get("sql"),
        "columns": columns,
        "rows": rows,
        "chart_data": chart,
        "summary": summary,
        "doc_name": doc_name,
        "scope": scope,
    }
    from ..services import agent as agent_service

    pictured = agent_service._images_payload(title, result)
    if pictured["images"] or pictured["missing_images"]:
        payload.update(
            {
                "images": pictured["images"],
                "image_items": pictured["image_items"],
                "missing_images": pictured["missing_images"],
            }
        )
    return payload


class CardStateRequest(BaseModel):
    doc_name: str
    pinned: bool | None = None
    on_board: bool | None = None
    group_name: str | None = None


@router.post("/dashboard/card-state", dependencies=[Depends(require_token)])
def dashboard_card_state(payload: CardStateRequest) -> dict:
    """置顶 / 分组 / 移出看板（移出只是不在看板显示，示例本身还在）。"""
    if str(payload.doc_name).startswith("template:"):
        return {"ok": False, "detail": "系统模板不可改状态"}
    ok = update_board_card(
        payload.doc_name,
        pinned=payload.pinned,
        board=payload.on_board,
        group_name=payload.group_name,
    )
    return {"ok": ok}


@router.get("/dashboard/sync-status", dependencies=[Depends(require_token)])
def dashboard_sync_status() -> dict:
    """示例库同步状态：本地条数 / 已确认 / RAGFlow 文档数（管理页展示）。"""
    from ..services import nl2sql

    return nl2sql.example_sync_status()


@router.delete("/dashboard/cards/{doc_name}", dependencies=[Depends(require_token)])
def dashboard_card_delete(doc_name: str) -> dict:
    """**真删**一张固定看板卡片：RAGFlow「SQL问答示例」+「SQL问题索引」+ 本地示例表 + 历史。

    和"移出"（`on_board=False`，示例还留着、以后还能复用）是两回事——答错的示例要真删，
    否则它还会被 few-shot / 示例直达召回（用户要求）。这里**不动模板**：模板删不删由
    管理页那条链路自己决定。
    """
    from ..services import templatizer

    name = str(doc_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="doc_name 必填")
    if name.startswith("template:"):
        raise HTTPException(status_code=400, detail="系统模板不是示例，不能这样删")
    result = templatizer.purge_sources([name])
    if not (result.get("removed_records") or result.get("deleted_docs")):
        raise HTTPException(status_code=404, detail=f"没找到这条示例：{name}")
    log_event("dashboard", "INFO", "card_deleted", doc_name=name, **result)
    return {"ok": True, "doc_name": name, **result}


@router.post("/dashboard/purge-logs", dependencies=[Depends(require_token)])
def dashboard_purge_logs() -> dict:
    """手动触发一次本地库清理（定时任务每 LOG_PURGE 小时也会自己跑一次）。"""
    from .. import store
    from ..services.logging_service import rotate_log_files

    before = store.database_size_bytes()
    deleted = store.purge_old_records()
    logs = rotate_log_files()
    vacuumed = False
    if sum(int(value or 0) for value in deleted.values()):
        vacuumed = store.vacuum_database()
    return {
        "deleted": deleted,
        "logs": logs,
        "vacuumed": vacuumed,
        "before_mb": round(before / 1048576, 1),
        "after_mb": round(store.database_size_bytes() / 1048576, 1),
    }


@router.post("/dashboard/sync-examples", dependencies=[Depends(require_token)])
def dashboard_sync_examples() -> dict:
    """立即把 RAGFlow 示例库里本地没有的问答对补进来（幂等），并顺带对齐两个数据集。

    同步之后：① 「SQL问题索引」集对账一次（两个集同进同出）；② 后台跑一次**增量**挖掘，
    新进来的问答对会被尝试转成模板，成了的会自己出现在快捷查询里。
    """
    from ..services import nl2sql, templatizer

    imported = nl2sql.import_ragflow_examples(force=True)
    # "给 MDL 示例补物理 SQL"不再依赖前端按钮：对账时自动跑（失败静默、下次再试）
    backfilled: dict = {}
    try:
        backfilled = nl2sql.backfill_physical_sql()
    except Exception as exc:  # noqa: BLE001
        from ..services.logging_service import log_error

        log_error("dashboard", "sync_backfill_physical_failed", exc)
    question_index: dict = {}
    try:
        question_index = nl2sql.backfill_question_index()
    except Exception as exc:  # noqa: BLE001
        from ..services.logging_service import log_error

        log_error("dashboard", "sync_question_index_failed", exc)
    # 增量挖掘放后台：解析 + 回环验证要跑几秒，不占这次请求的时间
    templatizer.mine_in_background("templatize-after-sync")
    return {
        "imported": imported,
        "physical_backfill": backfilled,
        "question_index": question_index,
        "status": nl2sql.example_sync_status(),
    }
