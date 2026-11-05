"""通用能力接口：把字段目录 / 通用明细 / 通用算子 / 数据画像做成**可传参**的前端入口。

见 docs/通用模板扩充与全字段明细方案.md 第二、三、四节 —— 这些能力以前只在
对话的候选确认卡里被动命中，现在快捷查询页也能自己选表、选字段、拼条件跑。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from ..config import settings
from ..services import generic, schema_catalog as catalog, sql_engine
from ..services.logging_service import log_error

router = APIRouter(prefix="/api")

_SAFE_VALUE = re.compile(r"^[0-9A-Za-z_\-:\.，,\u4e00-\u9fff ]*$")


def require_token(request: Request, token: str | None = Header(default=None)) -> None:
    expected = [item.strip() for item in str(settings.widget_token or "").split(",") if item.strip()]
    if not expected:
        return
    given = str(token or request.query_params.get("token") or "").strip()
    if given not in expected:
        raise HTTPException(status_code=401, detail="token 无效或缺失")


class FixtureProbe(BaseModel):
    """「在测试库上试这条 SQL」：只把 SQL 丢给**本地夹具库**（SQLite），不碰业务库。"""

    sql: str = ""
    kind: str = "mysql"


@router.post("/fixtures/probe", dependencies=[Depends(require_token)])
def fixtures_probe(payload: FixtureProbe) -> dict:
    """在夹具库上跑一条 SQL，回三态结论。

    安全边界：这里**只调用 fixture_lab**（本地 SQLite，结构生成的 18+2 张表 × 12 行），
    既不对 MySQL/PG 执行任何语句、也不写任何东西；所以允许前端把"刚才那条 SQL"回传上来，
    不会成为"前端可提交任意 SQL 打真库"的口子。
    """
    from ..services import fixture_lab

    kind = payload.kind if payload.kind in {"mysql", "postgresql"} else "mysql"
    result = fixture_lab.probe(payload.sql, kind)
    labels = {
        "ok": "夹具库跑通",
        "empty": "夹具库能跑但 0 行",
        "unsupported": "夹具库不适用（SQLite 方言差异）",
        "error": "夹具库报错",
        "unavailable": "夹具库不可用",
    }
    result["label"] = labels.get(str(result.get("status")), str(result.get("status")))
    return result


@router.get("/generic/catalog", dependencies=[Depends(require_token)])
def generic_catalog() -> dict:
    """给前端下拉用：白名单表 + 每张表的列（中文名、类型、编码映射、角色）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for kind in ("mysql", "postgresql"):
        tables = []
        for entry in catalog.field_catalog(kind).get("tables") or []:
            tables.append(
                {
                    "table": entry.get("table"),
                    "label": entry.get("label"),
                    "columns": [
                        {
                            "name": col.get("name"),
                            "label": col.get("label"),
                            "type": col.get("type"),
                            "roles": col.get("roles") or [],
                            "code_map": col.get("code_map") or {},
                        }
                        for col in entry.get("columns") or []
                    ],
                }
            )
        out[kind] = tables
    return {"catalog": out, "operators": OPERATORS}


OPERATORS = [
    {"id": "detail", "label": "通用明细（任意表 + 任意列 + 任意条件）", "need_column": False},
    {"id": "count", "label": "条件计数（任意表 + 任意条件数数）", "need_column": False},
    {"id": "group", "label": "分组分布（按某列分组 + 占比）", "need_column": True},
    {"id": "time_series", "label": "时间序列（按年/季/月/周/日）", "need_column": True},
    {"id": "rank", "label": "Top N（按某列分组取前几名）", "need_column": True},
    {"id": "stats", "label": "数值列统计（合计/平均/最大/最小）", "need_column": False},
    {"id": "nulls", "label": "空值率 / 填报完整度", "need_column": False},
    {"id": "distinct", "label": "取值清单（编码翻中文）", "need_column": True},
    {"id": "geo", "label": "经纬度范围", "need_column": False},
    {"id": "profile", "label": "数据画像（全库/单表哪些字段没数据）", "need_column": False},
]


@router.get("/templates/registry", dependencies=[Depends(require_token)])
def template_registry() -> dict:
    """三类确定性能力的统一注册表（docs/代码结构重构与模板外置方案.md §5.2）。

    builtin=代码内置模板（本阶段只读可见）、preset=前端自建模板、generic=通用算子。
    """
    from ..services import preset_lib
    from ..services import templatizer
    from ..services.resource_loader import load_yaml
    from ..store import list_recorded_example_pairs

    # doc 名 → 问题原文：维护页删除时要让人看清"删的是哪几条问答"，只给 pair-xxx.md 认不出来
    try:
        example_questions = {
            str(row.get("doc_name")): str(row.get("question") or "")
            for row in list_recorded_example_pairs(limit=5000)
        }
    except Exception:  # noqa: BLE001
        example_questions = {}

    registry = load_yaml("sql_templates/_index.yaml", {}) or {}
    builtin, listed_presets, generic_items = [], [], []
    for item in registry.get("templates") or []:
        source = str((item or {}).get("source") or "builtin")
        if source == "generic":
            generic_items.append({"id": item.get("id"), "title": item.get("title"), "source": "generic"})
        elif source == "preset":
            listed_presets.append({"id": item.get("id"), "title": item.get("title"), "source": "preset"})
        else:
            builtin.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "dialect": item.get("dialect") or "physical",
                    "trigger": item.get("trigger") or "",
                    "source": "builtin",
                }
            )
    presets = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "category": item.get("category"),
            "params": item.get("params") or [],
            "source": "preset",
        }
        for item in (preset_lib.load().get("queries") or [])
    ]
    builtin_items = builtin or [
        {"id": name, "title": label, "dialect": "physical", "trigger": "见构造器", "source": "builtin"}
        for name, label in (
            ("_build_metric_top_template", "按指标取极值（并列出并列）"),
            ("_build_density_template", "病害密度"),
            ("_build_per_km_template", "每公里指标（密度/间距）"),
            ("_build_pile_bucket_template", "按桩号分段统计"),
            ("_build_bucket_template", "按指标中位数分两档对比"),
            ("_build_generic_metric_template", "通用「维度 × 指标」聚合"),
            ("_build_route_info_template", "路线台账信息"),
            ("_build_average_template", "平均值 + 超过平均值的条数"),
            ("_build_distinct_pile_template", "不同桩号计数"),
            ("_build_quality_template", "时间跨度/字段缺失等质量统计"),
        )
    ]
    # 模板 → 源头示例：维护页删除按钮据此提示"会连源头一起清掉"
    auto_sources: dict[str, list[dict[str, str]]] = {}
    for doc_name, entry in (templatizer.load().get("mined") or {}).items():
        owner = str((entry or {}).get("template") or "")
        if owner:
            auto_sources.setdefault(owner, []).append(
                {
                    "doc_name": str(doc_name),
                    "question": example_questions.get(str(doc_name), ""),
                }
            )
    auto_items = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "question": item.get("question"),
            "status": item.get("status"),
            # 口径风控（计数可能漏算等）：非空就是"待观察 + 人工核对"的理由
            "risk": item.get("risk") or "",
            "enabled": bool(item.get("enabled", True)),
            "usage": int(item.get("usage") or 0),
            "variants": item.get("variants") or [],
            # 维护页展开抽屉要看的东西：参数化 SQL / 匹配自画像 / 样本 / 指纹
            "sql": item.get("sql") or "",
            "match": item.get("match") or {},
            "samples": item.get("samples") or [],
            "fingerprint": item.get("fingerprint") or "",
            "created_at": item.get("created_at") or "",
            "params": [
                {
                    "name": name,
                    "label": spec.get("label") or name,
                    "type": spec.get("type"),
                    "confirmed_by": spec.get("confirmed_by") or "domain",
                    "value": (item.get("samples") or [{}])[0].get("values", {}).get(name, ""),
                }
                for name, spec in (item.get("params") or {}).items()
            ],
            "conditions": (item.get("match") or {}).get("conditions") or [],
            "fixed": (item.get("match") or {}).get("fixed") or {},
            "source_docs": auto_sources.get(str(item.get("id")), []),
            # 完整性标记：这条模板的问法有多个子问题、模板只覆盖一部分（方案 §4.3）
            "partial": bool(item.get("partial")),
            # LLM 审核结论（方案 §5.1）与生命周期原因（§5.3）
            "review": item.get("review") or {},
            "broken_reason": str(item.get("broken_reason") or ""),
            "cold_reason": str(item.get("cold_reason") or ""),
            "demote_reason": str(item.get("demote_reason") or ""),
            "source": "auto",
        }
        for item in templatizer.list_templates()
    ]
    rejects = list(templatizer.load().get("rejects") or [])
    # 内置模板注册表自检：_index.yaml 里 builder 写错/漏写、或实现了却没人指向
    try:
        from ..services import nl2sql

        registry_issues = nl2sql.builtin_registry_issues()
    except Exception:  # noqa: BLE001
        registry_issues = []
    return {
        "builtin": builtin_items,
        "presets": presets,
        "listed_presets": listed_presets,
        "generic": generic_items or [{"id": op["id"], "title": op["label"], "source": "generic"} for op in OPERATORS],
        # L1 自动沉淀模板（docs/RAG示例直达与AI直答旁路方案.md §2.9）
        "auto": auto_items,
        "rejects": rejects,
        "registry_issues": registry_issues,
        "counts": {
            "builtin": len(builtin_items),
            "presets": len(presets),
            "generic": len(generic_items) or len(OPERATORS),
            "auto": len(auto_items),
            "rejects": len(rejects),
            "registry_issues": len(registry_issues),
        },
    }


class AutoTemplateRequest(BaseModel):
    id: str
    enabled: bool | None = None


@router.post("/templates/auto", dependencies=[Depends(require_token)])
def auto_template_update(payload: AutoTemplateRequest) -> dict:
    """停用/启用一条自动模板（停用后不再进对话候选；不影响已有示例）。"""
    from ..services import templatizer

    if payload.enabled is None:
        raise HTTPException(status_code=400, detail="enabled 必填")
    if not templatizer.set_enabled(payload.id, payload.enabled):
        raise HTTPException(status_code=404, detail=f"没有这条自动模板：{payload.id}")
    return {"saved": True, "id": payload.id, "enabled": payload.enabled}


@router.delete("/templates/auto/{template_id}", dependencies=[Depends(require_token)])
def auto_template_delete(template_id: str, purge_source: int = 0) -> dict:
    """删除一条自动模板。

    purge_source=1：**连源头示例一起删**（RAGFlow「SQL问答示例」+「SQL问题索引」+
    本地示例表 + 历史记录）——这是维护页删除按钮的行为（用户要求）。只删模板的话，
    用户还得自己去 RAGFlow 里找那条问答删掉，而且点一次「全量重挖」它就从源头又长回来。
    源头 = 挖掘时记在「已挖掘台账」里的 doc 名（一条模板可能合并了多条问答）。
    """
    from ..services import templatizer

    if purge_source:
        result = templatizer.delete_with_sources(template_id)
        if not result.get("deleted"):
            raise HTTPException(status_code=404, detail=f"没有这条自动模板：{template_id}")
        return result
    if not templatizer.delete(template_id):
        raise HTTPException(status_code=404, detail=f"没有这条自动模板：{template_id}")
    return {"deleted": True, "id": template_id}


@router.post("/templates/auto/review", dependencies=[Depends(require_token)])
def auto_template_review(limit: int = 3) -> dict:
    """LLM 审核还没审过的模板（方案 §5.1）：只审不修，有 high 级问题就降级回 candidate。

    限量默认 3 条，控制花费；挖掘在后台线程里也会自动补审（每次 2 条）。
    """
    from ..services import templatizer

    return templatizer.review_pending(limit=limit)


@router.post("/templates/auto/sweep", dependencies=[Depends(require_token)])
def auto_template_sweep() -> dict:
    """模板生命周期巡检（方案 §5.3）：

      · broken：SQL 引用的表/列在当前字段目录里找不到 → 不进推荐，等人工更新；
      · cold：被摆出来 ≥ TEMPLATE_COLD_AFTER_OFFERS 次却一次没被复用 → 不进推荐但保留。
    只改状态、不改 SQL（§八）。
    """
    from ..services import templatizer

    return templatizer.sweep_templates()


@router.post("/templates/auto/drop-multi-object", dependencies=[Depends(require_token)])
def auto_template_drop_multi_object() -> dict:
    """清掉"多对象对比"沉淀出来的自动模板（方案 §2.2-6：这类不许沉淀，只走相似问法）。

    挖掘闸已经拦住新的；这个入口用于清理存量（换库/重新同步示例后可以再跑一次）。
    """
    from ..services import templatizer

    return templatizer.drop_multi_object_templates()


@router.post("/templates/auto/rebuild", dependencies=[Depends(require_token)])
def auto_template_rebuild() -> dict:
    """全量重挖：清空"已挖掘台账"，按最新规则把示例库里每条问答都重算一遍。"""
    from ..services import templatizer

    return templatizer.mine_all(force=True, manual=True)


@router.post("/templates/auto/refresh", dependencies=[Depends(require_token)])
def auto_template_refresh() -> dict:
    """模板刷新（增量）：只尝试**新加进示例库/RAGFlow 的那批问答**。

    已经挖过的直接跳过、已有模板原样保留（不重算、不降级、promoted_at 不丢），
    所以这个按钮可以放心频繁点。手动触发不受"自动挖掘"开关限制。
    """
    from ..services import templatizer

    return templatizer.mine_all(manual=True)


@router.post("/templates/auto/recalibrate", dependencies=[Depends(require_token)])
def auto_template_recalibrate(apply: int = 0) -> dict:
    """模板 spec 重校准：按**当前**的 SQL 自画像重算每条模板的 shapes/主体/分组/指标。

    只修"挖掘器后来改好了识别、但存量记录不会跟着变"造成的老记录（例如把
    `problem_type_name` 认成病害类型维度、`LIMIT 占位` 认成取前 N），不重挖 SQL、
    不动条件与槽位。`apply=0`（默认）只回报差异，`apply=1` 才写回。
    """
    from ..services import templatizer

    report = templatizer.recalibrate_specs(dry_run=not bool(apply))
    updated = [item for item in report if item.get("action") == "updated"]
    return {
        "applied": bool(apply),
        "total": len(report),
        "updated": len(updated),
        "skipped": sum(1 for item in report if item.get("action") == "skipped"),
        "changes": updated,
    }


@router.get("/templates/auto", dependencies=[Depends(require_token)])
def auto_templates(with_sql: int = 0, runnable_only: int = 1, status: str = "") -> dict:
    """自动沉淀模板清单（快捷查询与维护页共用）。

    runnable_only=1（默认）只回"能一键点查"的：mature + enabled；
    维护页传 runnable_only=0 看全部（含 candidate），status= 可按状态过滤。
    SQL 默认不回（安全红线同 preset：前端不能提交自定义 SQL），with_sql=1 才给。
    """
    from ..services import templatizer

    wanted = str(status or "").strip()
    # 模板 → 源头示例（doc 名 + 问题原文）：维护页据此提示"删这条会连它的源头一起清掉"
    sources: dict[str, list[dict[str, str]]] = {}
    for doc_name, entry in (templatizer.load().get("mined") or {}).items():
        owner = str((entry or {}).get("template") or "")
        if owner:
            sources.setdefault(owner, []).append({"doc_name": str(doc_name), "question": ""})
    items: list[dict[str, Any]] = []
    for template in templatizer.list_templates():
        state = str(template.get("status") or "")
        if wanted and state != wanted:
            continue
        if runnable_only and (state != "mature" or not template.get("enabled", True)):
            continue
        spec = template.get("match") or {}
        item: dict[str, Any] = {
            "id": str(template.get("id")),
            "title": str(template.get("title") or template.get("id")),
            "question": str(template.get("question") or ""),
            "scope": "自动沉淀模板："
            + str(template.get("question") or template.get("id"))[:40]
            + "（0 次大模型）",
            "variants": list(template.get("variants") or []),
            "status": state,
            "enabled": bool(template.get("enabled", True)),
            "usage": int(template.get("usage") or 0),
            "dialect": str(template.get("dialect") or "physical"),
            "conditions": list(spec.get("conditions") or []),
            "fixed": dict(spec.get("fixed") or {}),
            "created_at": str(template.get("created_at") or ""),
            "source_docs": sources.get(str(template.get("id")), []),
            # 表单描述：entity/enum/time/int 带 options（连库现查），numeric/op 交给前端渲染输入框
            "params": templatizer.form_params(template),
        }
        if with_sql:
            item["sql"] = str(template.get("sql") or "")
        items.append(item)
    return {"templates": items, "count": len(items)}


class AutoRunRequest(BaseModel):
    id: str
    params: dict[str, str] = {}


@router.post("/templates/auto/run", dependencies=[Depends(require_token)])
def auto_template_run(payload: AutoRunRequest) -> dict:
    """执行一条自动沉淀模板：前端只传 id + 参数值，SQL 由后端绑定（对齐 /api/preset-query）。"""
    from ..services import nl2sql, templatizer

    template = templatizer.find(payload.id)
    if not template:
        raise HTTPException(status_code=404, detail=f"没有这条自动模板：{payload.id}")
    if str(template.get("status")) != "mature" or not template.get("enabled", True):
        raise HTTPException(status_code=403, detail="这条模板还没转正或已停用，不能直接执行。")
    sql, reason = templatizer.bind(template, payload.params or {})
    if not sql:
        raise HTTPException(status_code=400, detail=reason)
    # 只读兜底：模板 SQL 是我们自己挖出来的，但执行前仍按 preset 的规矩再校验一次
    ok, why, clean = nl2sql.sql_engine_validate(sql, "mysql")
    if not ok:
        raise HTTPException(status_code=400, detail=f"模板 SQL 校验没通过：{why}")
    try:
        result = sql_engine.execute_sql("mysql", clean or sql)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"自动模板执行失败：{exc}") from exc
    templatizer.bump_usage(str(template.get("id")))
    rows = result.get("rows") or []
    columns = result.get("columns") or []
    title = str(template.get("title") or template.get("id"))
    if not rows:
        reply = "未查询到符合条件的数据。"
    elif len(rows) == 1 and len(columns) == 1:
        reply = f"{title}：{rows[0].get(columns[0])}"
    else:
        reply = f"{title}（共 {len(rows)} 行，见结果表格）"
    scope = f"自动沉淀模板：{str(template.get('question') or title)[:40]}（0 次大模型）"
    return {
        "reply": f"{reply}\n\n统计口径：\n- {scope}",
        "source": "auto",
        "engine": "auto",
        "template_id": str(template.get("id")),
        "sql": result.get("sql"),
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "scope": scope,
    }


class ExampleBackfillRequest(BaseModel):
    kind: str = "physical_sql"


@router.post("/examples/backfill", dependencies=[Depends(require_token)])
def example_backfill() -> dict:
    """给示例库里的 Wren MDL-SQL 补一份编译好的物理 SQL（重放不再依赖 Wren）。"""
    from ..services import nl2sql

    return nl2sql.backfill_physical_sql()


@router.post("/examples/repair-spec-order", dependencies=[Depends(require_token)])
def example_repair_spec_order() -> dict:
    """修"示例 spec 里的路线顺序和归一化 SQL 占位符顺序不一致"的存量示例。

    顺序不一致时示例复用会把参数填反（`{route_id}` 和 `{route_id_2}` 对调），
    实测出现"标签写西乡、数据其实是恒明珠"。修不好的一律标成未确认，退回 Wren。
    """
    from ..services import nl2sql

    return nl2sql.repair_example_spec_order()


@router.post("/examples/question-index/sync", dependencies=[Depends(require_token)])
def question_index_sync(force: int = 0) -> dict:
    """对账「SQL问题索引」集：补齐缺失的问题、删掉问答示例集里已经没有的。

    两个集必须同进同出（问题索引只存问题正文，专门给相似问法召回用）。force=1 时全部重传。
    """
    from ..services import nl2sql

    return nl2sql.backfill_question_index(force=bool(force))


@router.post("/templates/auto/{template_id}/promote", dependencies=[Depends(require_token)])
def auto_template_promote(template_id: str) -> dict:
    """把 candidate（待观察）转正成 mature（可推荐）——只改状态，不动 SQL。"""
    from ..services import templatizer

    if not templatizer.promote(template_id):
        raise HTTPException(status_code=404, detail=f"没有这条自动模板：{template_id}")
    return {"promoted": True, "id": template_id}


@router.post("/templates/auto/{template_id}/remine", dependencies=[Depends(require_token)])
def auto_template_remine(template_id: str, apply: int = 0) -> dict:
    """按**当前**挖掘器重挖这一条模板的槽位（存量模板是旧版挖的，槽位不会自动跟着变）。

    实测"看交安事件的病害样图"当初把 `disease_categories_type = '15'` 当固定值，
    维护页显示"（无可换参数）"，而现在的挖槽器能挖出带 4 个选项的枚举参数。
    `apply=0`（默认）只回报会改成什么，`apply=1` 才写回。
    """
    from ..services import templatizer

    report = templatizer.remine_sql(template_id, dry_run=not bool(apply))
    if not report.get("ok") and "没有这条模板" in str(report.get("reason") or ""):
        raise HTTPException(status_code=404, detail=str(report.get("reason")))
    return report


@router.post(
    "/templates/auto/{template_id}/review/ignore", dependencies=[Depends(require_token)]
)
def auto_template_review_ignore(template_id: str) -> dict:
    """把这条模板的审核建议标成"已忽略"（用户判定为误报）——结论留着，界面不再提示。"""
    from ..services import templatizer

    if not templatizer.ignore_review(template_id):
        raise HTTPException(status_code=404, detail=f"没有这条自动模板的审核结论：{template_id}")
    return {"ignored": True, "id": template_id}


class GenericFilter(BaseModel):
    column: str
    op: str = "="
    value: Any = None


class GenericQuery(BaseModel):
    kind: str = "mysql"
    operator: str = "detail"
    table: str = ""
    columns: list[str] = []
    filters: list[GenericFilter] = []
    group_column: str = ""
    metric_column: str = ""
    agg: str = "count"
    time_unit: str = "month"
    top: int = 10
    order_column: str = ""
    order_direction: str = "desc"
    limit: int = 500
    only_empty: bool = False


def _check_values(filters: list[GenericFilter]) -> None:
    for item in filters:
        if isinstance(item.value, str) and not _SAFE_VALUE.match(item.value):
            raise HTTPException(status_code=400, detail=f"筛选值含非法字符：{item.value[:20]}")


@router.post("/generic/query", dependencies=[Depends(require_token)])
def generic_query(payload: GenericQuery) -> dict:
    """按选定的算子 + 参数拼 SQL 并执行（表/列/操作符全部白名单校验）。"""
    kind = payload.kind if payload.kind in {"mysql", "postgresql"} else "mysql"
    operator = str(payload.operator or "detail")
    filters = [
        {"column": item.column, "op": item.op, "value": item.value}
        for item in payload.filters
        if str(item.column or "").strip()
    ]
    _check_values(payload.filters)
    if operator == "profile":
        table = catalog.resolve_table(kind, payload.table) if payload.table else None
        result = generic.profile_columns(kind, table=table, only_empty=payload.only_empty)
        return {
            "reply": (
                f"{'表 ' + table + ' 的' if table else '全库'}字段画像："
                f"{len(result.get('empty_tables') or [])} 张空表、"
                f"{len(result.get('columns') or [])} 个空值字段"
            ),
            "engine": "profiling",
            "columns": ["表", "字段", "类型", "总行数", "空值数", "空值率"],
            "rows": [
                {
                    "表": item["table_label"],
                    "字段": item["label"],
                    "类型": item["type"],
                    "总行数": item["total"],
                    "空值数": item["nulls"],
                    "空值率": item["null_rate"],
                }
                for item in result.get("columns") or []
            ],
            "sql": None,
            "scope": "数据画像（判空口径：数值/日期只判 IS NULL，字符型判空串）",
        }
    table = catalog.resolve_table(kind, payload.table) if payload.table else None
    if not table:
        raise HTTPException(status_code=400, detail=f"表「{payload.table}」不在白名单里")
    if operator == "detail":
        sql, scope = generic.generic_detail(
            kind, table, payload.columns, filters,
            {"column": payload.order_column, "direction": payload.order_direction}
            if payload.order_column else None,
            payload.limit,
        )
    elif operator == "count":
        # 「任意表 + 任意条件数数」：服务层 generic_count 一直是现成的（对话候选在用），
        # 只是快捷查询的算子清单里漏了它 → operator=count 落进"不支持的算子"报 400。
        # 计数要的是单值 COUNT(*)，明细那套 LIMIT 截断会报错总数，所以单独一个算子。
        sql, scope = generic.generic_count(kind, table, filters)
    elif operator == "group":
        sql, scope = generic.group_distribution(kind, table, payload.group_column)
    elif operator == "time_series":
        sql, scope = generic.time_series(kind, table, payload.group_column, payload.time_unit)
    elif operator == "rank":
        sql, scope = generic.rank(
            kind, table, payload.group_column, payload.metric_column, payload.agg, payload.top
        )
    elif operator == "stats":
        sql, scope = generic.column_stats(kind, table, payload.columns)
    elif operator == "nulls":
        sql, scope = generic.null_completeness(kind, table, payload.columns)
    elif operator == "distinct":
        sql, scope = generic.distinct_values(kind, table, payload.group_column)
    elif operator == "geo":
        sql, scope = generic.geo_bounds(kind, table, filters)
    else:
        raise HTTPException(status_code=400, detail=f"不支持的算子：{operator}")
    if not sql:
        raise HTTPException(status_code=400, detail=scope or "这组参数拼不出可执行的查询")
    try:
        result = sql_engine.execute_sql(kind, sql)
    except Exception as exc:  # noqa: BLE001
        log_error("generic_api", "execute_failed", exc, operator=operator, table=table)
        raise HTTPException(status_code=400, detail=f"执行失败：{exc}") from exc
    rows = result.get("rows") or []
    return {
        "reply": f"{scope}（共 {len(rows)} 行）",
        "engine": "generic",
        "sql": result.get("sql"),
        "columns": result.get("columns") or [],
        "rows": rows,
        "scope": scope,
        "row_count": len(rows),
    }
