"""预置查询：验证过的高频问题一键出数（不走 LLM，毫秒级、结果确定）。

见 docs/产品化与结果可信度整改方案.md 第五章第 1 节。
安全红线：前端只能传「查询 id + 参数值」，参数值必须命中模板里声明的 options 白名单，
SQL 本身永远不出后端；拼进 SQL 的值只允许 [0-9A-Za-z_-]，从根上杜绝注入。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..config import PROJECT_ROOT, settings
from ..services import sql_engine
from ..services import preset_lib

router = APIRouter(prefix="/api")

_PRESET_FILE = Path(__file__).resolve().parents[1] / "data" / "preset_queries.json"


def _load() -> dict[str, Any]:
    return preset_lib.load()  # 带版本号，改文件即时生效（服务层统一负责热重载）


def _query_by_id(query_id: str) -> dict[str, Any]:
    item = preset_lib.query_by_id(query_id)
    if item:
        return item
    raise HTTPException(status_code=404, detail=f"没有这个预置查询：{query_id}")


def _month_label(value: str) -> str:
    return preset_lib.month_label(value)


def _dynamic_options(source: str) -> list[dict[str, str]]:
    """下拉框选项**每次连库现查**（去重），不写死在配置里。

    路线只列"确实有病害数据"的路线：台账里 3623 条路线，全塞进下拉框没法用；
    病害数和月份、病害类型都由库里的数据决定，数据变了选项自动跟着变。
    """
    return preset_lib._dynamic_options(source)


def _options_for(name: str, spec: dict[str, Any]) -> list[dict[str, str]]:
    """参数的候选取值：配了 source 就连库现查，否则用静态 options。"""
    return preset_lib.options_for(name, spec)


def _bind(query: dict[str, Any], values: dict[str, str]) -> str:
    """把参数绑进模板：只接受声明过的参数、只接受 options 白名单里的值。"""
    sql, reason = preset_lib.bind(query, values)
    if not sql:
        raise HTTPException(status_code=400, detail=reason)
    return sql


def require_token(
    request: Request, token: str | None = Header(default=None)
) -> None:
    """轻量鉴权（配了 WIDGET_TOKEN 才校验）：请求头 token 或 URL 参数 ?token=。"""
    # 支持多个口令（逗号分隔），方便给不同宿主/环境各发一个
    expected = [item.strip() for item in str(settings.widget_token or "").split(",") if item.strip()]
    if not expected:
        return
    given = str(token or request.query_params.get("token") or "").strip()
    if given not in expected:
        raise HTTPException(status_code=401, detail="token 无效或缺失")


@router.get("/preset-queries", dependencies=[Depends(require_token)])
def preset_queries(with_sql: int = 0) -> dict:
    """给前端摆问题卡片用：查询清单 + 参数选项。

    默认**不带 SQL**（安全红线：前端不能提交自定义 SQL 执行）；
    管理页传 with_sql=1 时才回，用来回填编辑框、当仿写样例
    （见 docs/固定看板重构与收藏固化方案.md 第三章）。
    """
    data = _load()
    specs = data.get("params") or {}
    include_sql = bool(with_sql)
    return {
        "params": {
            # type 要透给前端：`type: time/date` 的参数用日期选择器，其余用下拉/输入框
            name: {
                "label": spec.get("label") or name,
                **({"type": spec["type"]} if spec.get("type") else {}),
                **({"hint": spec["hint"]} if spec.get("hint") else {}),
                "options": _options_for(name, spec),
            }
            for name, spec in specs.items()
        },
        "queries": [
            {
                "id": item.get("id"),
                "category": item.get("category"),
                "title": item.get("title"),
                "question": item.get("question"),
                "params": item.get("params") or [],
                "scope": item.get("scope") or "",
                **({"sql": item.get("sql") or ""} if include_sql else {}),
            }
            for item in data.get("queries") or []
        ],
    }


class PresetRequest(BaseModel):
    id: str
    params: dict[str, str] = {}


@router.post("/preset-query", dependencies=[Depends(require_token)])
def run_preset(payload: PresetRequest) -> dict:
    """执行预置查询：模板来自后端白名单，参数只做值绑定。"""
    query = _query_by_id(payload.id)
    sql = _bind(query, payload.params)
    try:
        result = sql_engine.execute_sql("mysql", sql)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"预置查询执行失败：{exc}") from exc
    rows = result.get("rows") or []
    columns = result.get("columns") or []
    if not rows:
        reply = "未查询到符合条件的数据。"
    elif len(rows) == 1 and len(columns) == 1:
        reply = f"{query.get('title')}：{rows[0].get(columns[0])}"
    else:
        reply = f"{query.get('title')}（共 {len(rows)} 行，见结果表格）"
    return {
        "reply": f"{reply}\n\n统计口径：\n- {query.get('scope') or '按预置口径'}",
        "source": "preset",
        "engine": "preset",
        "preset_id": payload.id,
        "sql": result.get("sql"),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "scope": query.get("scope") or "",
    }


class PresetUpsert(BaseModel):
    query: dict


class DraftRequest(BaseModel):
    title: str = ""
    question: str = ""
    category: str = ""
    params: list[str] = []


@router.post("/preset-queries/draft", dependencies=[Depends(require_token)])
def preset_draft(payload: DraftRequest) -> dict:
    """让 AI 写一版预置查询草稿（只填表单，人工确认后才会保存生效）。"""
    from ..services import prompts
    from ..services.llm import LLMError, chat_json

    data = _load()
    samples = "\n".join(
        f"- {item.get('title')}（分类 {item.get('category')}，参数 {item.get('params') or '无'}）：\n"
        f"  问法：{item.get('question')}\n  SQL：{item.get('sql')}"
        for item in (data.get("queries") or [])[:6]
    )
    param_help = "\n".join(
        f"- {{{name}}}：{spec.get('label')}"
        + ("（连库现查）" if spec.get("source") else "（固定选项）")
        for name, spec in (data.get("params") or {}).items()
    )
    allowed = "、".join(f"{{{name}}}" for name in (data.get("params") or {}))
    messages = [
        {
            "role": "system",
            "content": (
                "你是道路巡检数据平台的 SQL 模板助手。根据用户给的标题/问法，产出一版**预置查询模板**草稿。"
                "只输出 JSON，字段：id（英文小写下划线）、category、title、question、params（数组）、sql、scope。"
                f"SQL 只能用占位符 {allowed}，并且只允许 SELECT/WITH 单条查询；用不到的占位符不要写。"
                "params 里列出的占位符必须都在 SQL 中出现，SQL 里的占位符也必须都在 params 里。"
                "表结构参考：病害 ai_problem（routes_id/damage_type/area/collection_time/pile_distance/"
                "up_or_down/del_flag），路线 basic_facilities_routes（id/short_name/start_num/end_num），"
                "病害类型字典 ai_problem_base（problem_code/problem_type）。"
                "注意：排查空/无用列时不要臆造列名。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"标题：{payload.title}\n问法：{payload.question}\n分类：{payload.category}\n"
                f"打算用的参数：{payload.params}\n\n"
                f"=== 现有模板（照着风格写）\n{samples}\n\n=== 可用参数\n{param_help}"
            ),
        },
    ]
    try:
        draft = chat_json(messages)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"AI 草稿生成失败：{exc}") from exc
    if not isinstance(draft, dict):
        raise HTTPException(status_code=502, detail="AI 草稿格式不对，请重试。")
    return {"draft": draft}


def _write(data: dict) -> None:
    """把整个预置表落盘（保留 _note 字段）。"""
    _PRESET_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    preset_lib.load(force=True)  # 立即重建内存索引，能力路由下一次就能命中


@router.post("/preset-queries", dependencies=[Depends(require_token)])
def preset_upsert(payload: PresetUpsert) -> dict:
    """新增/更新一条预置查询（id 相同即覆盖）。SQL 只存后端，前端仍只能传 id+参数。"""
    item = dict(payload.query or {})
    query_id = str(item.get("id") or "").strip()
    if not query_id or not str(item.get("sql") or "").strip():
        raise HTTPException(status_code=400, detail="id 和 sql 必填")
    data = json.loads(_PRESET_FILE.read_text(encoding="utf-8"))
    items = [entry for entry in (data.get("queries") or []) if entry.get("id") != query_id]
    item["params"] = [str(name) for name in (item.get("params") or [])]
    # 参数必须在 params 段里有定义，否则绑定时会 400
    for name in item["params"]:
        if name not in (data.get("params") or {}):
            raise HTTPException(status_code=400, detail=f"参数未定义：{name}")
    items.append(item)
    data["queries"] = items
    _write(data)
    return {"saved": True, "id": query_id, "count": len(items)}


@router.delete("/preset-queries/{query_id}", dependencies=[Depends(require_token)])
def preset_delete(query_id: str) -> dict:
    data = json.loads(_PRESET_FILE.read_text(encoding="utf-8"))
    items = [entry for entry in (data.get("queries") or []) if entry.get("id") != query_id]
    data["queries"] = items
    _write(data)
    return {"deleted": True, "count": len(items)}
