from __future__ import annotations

import asyncio
import contextlib
import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .routers import (
    chat,
    dashboard,
    export,
    generic_api,
    health,
    images,
    knowledge,
    models,
    preset,
    schema,
    storage,
    vendor,
)


app = FastAPI(title="道路 AI 巡检养护智能体", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # 浏览器规范禁止 credentials + 通配符来源同时使用；这里按配置来，默认不带凭证。
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(models.router)
app.include_router(knowledge.router)
app.include_router(export.router)
app.include_router(images.router)
app.include_router(vendor.router)
app.include_router(schema.router)
app.include_router(storage.router)
app.include_router(preset.router)
app.include_router(dashboard.router)
app.include_router(generic_api.router)


async def _reconcile_examples_forever() -> None:
    """示例库定时对账（方案第八章 §3）：把 RAGFlow 里新加的问答对补进本地表。

    对账幂等；RAGFlow 挂了就下一轮再试，不阻塞任何查询。
    """
    from .services import nl2sql
    from .services.logging_service import log_error, log_event

    minutes = float(os.getenv("EXAMPLE_RECONCILE_MINUTES", "30") or 30)
    if minutes <= 0:
        return
    while True:
        try:
            result = nl2sql.import_ragflow_examples(force=True, min_interval=0)
            if result.get("imported"):
                log_event("agent", "INFO", "example_reconcile", **{"imported": result["imported"]})
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "example_reconcile_failed", exc)
        # 顺手把示例库里的 Wren 逻辑 SQL 补成物理 SQL：重放/挖模板/看板刷新就都不再依赖
        # Wren 环境，也不用人去点按钮。Wren 不可用时静默失败，下一轮再试
        # （docs/通用算子过滤丢失与自动补物理SQL修复.md §二）。
        try:
            backfilled = nl2sql.backfill_physical_sql()
            if backfilled.get("compiled"):
                log_event(
                    "agent",
                    "INFO",
                    "example_physical_backfill",
                    count=len(backfilled["compiled"]),
                )
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "auto_backfill_physical_failed", exc)
        # 题目索引对账：示例库里新增/删除的题要同步到"只存问题"的题目库（相似问法召回走它）
        try:
            index_result = nl2sql.backfill_question_index()
            if index_result.get("added") or index_result.get("removed"):
                log_event("agent", "INFO", "question_index_backfill", **index_result)
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "question_index_backfill_failed", exc)
        # 归一化 SQL 对账：补上"可换参数重放"的版本（条件写死在 SQL 里的老示例）
        try:
            normalized = nl2sql.backfill_normalized_sqls()
            if normalized.get("updated"):
                log_event("agent", "INFO", "normalized_sql_backfill", **normalized)
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "normalized_sql_backfill_failed", exc)
        # 示例库有变化就顺手补挖自动模板（L1 的唯一事实源是 trusted 问答对，
        # 见 docs/RAG示例直达与AI直答旁路方案.md §2.5.2）：离线跑，不占问答请求。
        try:
            from .services import templatizer

            templatizer.mine_all()
        except Exception as exc:  # noqa: BLE001
            log_error("templatize", "reconcile_mine_failed", exc)
        await asyncio.sleep(minutes * 60)


async def _purge_local_db_forever() -> None:
    """定时清理本地库：日志表/历史表按保留期删旧记录，运行日志文件轮转。

    实测本地 agent.db 涨到 209MB（agent_logs 8 万行 + result_cache + query_history），
    这里只删旧的、不动近期记录：日志留 `LOG_RETENTION_DAYS` 天且最多 `LOG_MAX_ROWS` 行，
    历史留 `HISTORY_RETENTION_DAYS` 天，删完顺带 VACUUM 把文件空间还回去。
    """
    from . import store
    from .services.logging_service import log_error, log_event, rotate_log_files

    hours = float(getattr(settings, "db_purge_hours", 6) or 6)
    if hours <= 0:
        return
    while True:
        try:
            before = store.database_size_bytes()
            deleted = store.purge_old_records()
            logs = rotate_log_files()
            total_deleted = sum(int(value or 0) for value in deleted.values())
            # VACUUM 会锁库、耗时几秒，只在删得比较多时做（平时删掉的页会被复用，文件不会一直涨）
            if total_deleted >= 2000 and bool(getattr(settings, "db_vacuum_enabled", True)):
                store.vacuum_database()
            after = store.database_size_bytes()
            if total_deleted or logs.get("rotated") or logs.get("removed"):
                log_event(
                    "store",
                    "INFO",
                    "local_purge",
                    deleted=deleted,
                    logs=logs,
                    before_mb=round(before / 1048576, 1),
                    after_mb=round(after / 1048576, 1),
                )
        except Exception as exc:  # noqa: BLE001
            log_error("store", "local_purge_failed", exc)
        await asyncio.sleep(hours * 3600)


@app.on_event("startup")
async def _start_reconcile_task() -> None:
    # 资源自检（resources/ 里的词表、模板注册表能不能解析、id 有没有重复）
    try:
        from .services import resource_loader

        resource_loader.self_check()
    except Exception as exc:  # noqa: BLE001
        from .services.logging_service import log_error

        log_error("resources", "self_check_failed", exc)
    # 启动时对账一次（覆盖"直接往 RAGFlow 或本地表灌数据"），之后按周期重跑。
    app.state.reconcile_task = asyncio.create_task(_reconcile_examples_forever())
    # 本地库/日志定时清理：日志留几天、历史留几天，删完 VACUUM 还盘（本机库曾涨到 209MB）
    app.state.purge_task = asyncio.create_task(_purge_local_db_forever())
    # 预热校准事实（最常用路线的台账/数据范围）：第一条问某条路的问题不必现场查 3 张表。
    # 后台线程跑，不阻塞启动、失败静默（docs/复杂模板匹配与推荐改造方案.md 附带项）。
    threading.Thread(target=_warm_calibration, name="warm-calibration", daemon=True).start()


def _warm_calibration() -> None:
    try:
        from .services import calibrate

        calibrate.warm_cache()
    except Exception as exc:  # noqa: BLE001
        from .services.logging_service import log_error

        log_error("calibrate", "warm_cache_failed", exc)
    # 示例召回的两处冷启动开销也一起预热，免得第一条问题多等好几秒：
    #   1) 骨架词表（路线名 + 病害类型，要查库，实测 ~1.9s）；
    #   2) 本地示例 SQL → 物理 SQL 的判定（含 EXPLAIN，实测每条约 0.3s）。
    try:
        from .services import nl2sql

        nl2sql._skeleton_vocab()
        for row in nl2sql.list_recorded_example_pairs(limit=1000):
            sql = str(row.get("sql_text") or "").strip()
            if sql:
                nl2sql.physical_sql_of(sql, nl2sql.example_kind(row))
        nl2sql._ensure_example_dataset()
        nl2sql._ensure_question_dataset()
    except Exception as exc:  # noqa: BLE001
        from .services.logging_service import log_error

        log_error("agent", "warm_example_cache_failed", exc)


@app.on_event("shutdown")
async def _stop_reconcile_task() -> None:
    task = getattr(app.state, "reconcile_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

if settings.frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(settings.frontend_dist), html=True), name="frontend")

    @app.middleware("http")
    async def _no_store_index_html(request, call_next):
        """index.html 不许缓存：前端重新 build 之后，老页面不硬刷新也要能拿到新包。

        （实测踩过：静态资源带 hash 会自动换名，但已打开的标签页一直用内存里的老 JS，
        "重建语义层"旁边新加的生成器下拉怎么都不出现。）
        """
        response = await call_next(request)
        if request.url.path in {"", "/", "/index.html"}:
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response
