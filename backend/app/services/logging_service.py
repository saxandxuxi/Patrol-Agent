from __future__ import annotations

import json
import traceback
from datetime import datetime
from typing import Any

from ..config import settings
from ..store import get_connection


def log_path():
    """运行日志路径：logs/app/app.log（以前是 data/agent.log，和数据库混在一起）。"""
    directory = settings.log_dir / "app"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "app.log"


def rotate_log_files(max_mb: int | None = None, keep_days: int | None = None) -> dict[str, int]:
    """运行日志文件轮转 + 过期清理。

    app.log 超过上限就改名成 app.log.1（只留一个备份，上一份顺延成 app.log.2），
    顺带删掉超过保留期的轮转文件。日志同时写进 agent_logs 表，那份由
    `store.purge_old_records()` 按天/按行数清，两边一起管本地才不会再涨到几百 MB。
    """
    import time as _time

    limit_mb = int(max_mb if max_mb is not None else settings.log_file_max_mb)
    days = int(keep_days if keep_days is not None else settings.log_retention_days)
    base = log_path()
    rotated = 0
    removed = 0
    try:
        if base.exists() and limit_mb > 0 and base.stat().st_size > limit_mb * 1024 * 1024:
            second = base.with_name(base.name + ".2")
            if second.exists():
                second.unlink()
            first = base.with_name(base.name + ".1")
            if first.exists():
                first.rename(second)
            base.rename(first)
            rotated = 1
        cutoff = _time.time() - days * 86400
        for item in base.parent.glob(base.name + ".*"):
            if item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
    except Exception as exc:  # noqa: BLE001
        log_error("logging", "log_rotate_failed", exc)
    return {"rotated": rotated, "removed": removed}


def _ensure_table() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                level TEXT NOT NULL,
                module TEXT NOT NULL,
                event TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )


def log_event(module: str, level: str, event: str, **fields: Any) -> None:
    now = datetime.now().astimezone().isoformat(timespec="milliseconds")
    payload = {"time": now, "level": level, "module": module, "event": event, **fields}
    try:
        with log_path().open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    try:
        _ensure_table()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO agent_logs(time, level, module, event, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    now,
                    level,
                    module,
                    event,
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
    except Exception:
        pass


def log_error(module: str, event: str, error: Exception, **fields: Any) -> None:
    log_event(
        module,
        "ERROR",
        event,
        error=str(error),
        traceback=traceback.format_exc(limit=5),
        **fields,
    )
