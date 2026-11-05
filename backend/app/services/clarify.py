from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from ..config import settings
from .logging_service import log_error


class ClarifyNeeded(RuntimeError):
    """需要用户澄清字段或口径时抛出。"""

    def __init__(
        self,
        kind: str,
        reason: str,
        lead: str | None = None,
        show_fields: bool = True,
        choices: list[dict[str, Any]] | None = None,
        subject: str = "",
    ) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        # 自定义开场白：不同失败场景的追问方式不一样（空间口径 / 对比口径 / 字段口径）。
        self.lead = lead
        # SQL 根本没跑通时（例如 Unknown column），甩字段清单没用，得让用户看到真实报错。
        self.show_fields = show_fields
        # 选择式澄清（方案第七章 §7）：能点选的候选（例如"用这条已验证的查询 / 让 AI 重查"）。
        self.choices = choices or []
        # 主体（语义层判的）：追问文案按它过滤——问桥梁/隧道/边坡时不能再问"哪种病害"
        self.subject = str(subject or "")


@lru_cache(maxsize=4)
def _catalog(kind: str) -> dict[str, dict[str, Any]]:
    # 优先实时读库：建表/改字段/改注释后立即生效，不依赖本地缓存文件。
    try:
        from .schema_catalog import live_catalog

        live = live_catalog(kind, include_rows=False)
        tables = live.get("tables") or []
        if tables:
            return {
                str(table["table"]): {
                    "table": table["table"],
                    "comment": table.get("comment"),
                    "columns": [
                        {
                            "COLUMN_NAME": col.get("name"),
                            "COLUMN_TYPE": col.get("type"),
                            "IS_NULLABLE": col.get("nullable"),
                            "COLUMN_DEFAULT": col.get("default"),
                            "COLUMN_KEY": col.get("key"),
                            "COLUMN_COMMENT": col.get("comment"),
                        }
                        for col in table.get("columns", [])
                    ],
                }
                for table in tables
            }
    except Exception as exc:  # noqa: BLE001
        # 实时读库失败不致命（下面会退回本地生成的文件），但要留痕：
        # 以前这里直接 pass，字段目录莫名变空时只能靠猜
        log_error("clarify", "live_catalog_failed", exc, kind=kind)

    # 读库失败时退回本地生成的文件
    path = settings.data_dir / "schema_catalog.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log_error("clarify", "catalog_file_parse_failed", exc, file=str(path))
        return {}
    return {
        str(table.get("table")): table
        for table in data.get(kind, [])
        if table.get("table")
    }


def _allowed_tables(kind: str) -> tuple[str, ...]:
    if kind == "mysql":
        return settings.mysql_allowed_tables
    if kind == "postgresql":
        return settings.pg_allowed_tables
    # seaweedfs 是对象存储，没有表结构
    return ()


def fields(kind: str) -> list[dict[str, str]]:
    """展平字段清单：表 / 列名 / 类型 / 中文说明。"""
    catalog = _catalog(kind)
    rows: list[dict[str, str]] = []
    for table in _allowed_tables(kind):
        info = catalog.get(table)
        if not info:
            continue
        table_comment = str(info.get("comment") or "").strip()
        for col in info.get("columns", []):
            name = col.get("COLUMN_NAME") or col.get("column_name") or ""
            if not name:
                continue
            rows.append(
                {
                    "table": table,
                    "column": name,
                    "type": col.get("COLUMN_TYPE") or col.get("data_type") or "",
                    "comment": (col.get("COLUMN_COMMENT") or col.get("comment") or "").strip(),
                    "table_comment": table_comment,
                }
            )
    return rows


_CORE_TABLES = {
    "mysql": ("ai_problem", "ai_problem_base", "basic_facilities_routes"),
    "postgresql": ("gps_points", "inclination_points"),
}


def compact_fields(kind: str, comment_limit: int = 14) -> str:
    """给提示词用的紧凑字段速查：核心表带中文说明，其他表只列列名。"""
    rows = fields(kind)
    if not rows:
        return ""
    core = set(_CORE_TABLES.get(kind, ()))
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["table"], []).append(row)

    lines: list[str] = []
    for table, items in grouped.items():
        comment = items[0].get("table_comment") or ""
        head = f"- {table}（{comment}）" if comment else f"- {table}"
        if table in core:
            cells = "、".join(
                f"`{row['column']}`({row['comment'][:comment_limit]})"
                if row["comment"]
                else f"`{row['column']}`"
                for row in items
            )
        else:
            cells = ",".join(f"`{row['column']}`" for row in items)
        lines.append(f"{head}: {cells}")
    return "\n".join(lines)


def clarification_reply(
    kind: str,
    reason: str,
    lead: str | None = None,
    show_fields: bool = True,
    subject: str = "",
) -> str:
    lines: list[str] = []
    if lead:
        lines.append(lead)
        lines.append(f"（原因：{reason}）")
    else:
        lines.append(f"这条问题的查询条件我还差一点（{reason}），补一句就能查：")
    if show_fields:
        lines.extend(
            [
                "",
                "- 想查哪条路、哪段时间、哪种对象？（没说清楚的部分可以补充）",
            ]
        )
        example = "「西乡段」「7月」「泛油」" if not subject or subject == "disease" else "「哪条路」「全部时间」"
        lines.append(f"用大白话告诉我就行（比如 {example}），查询条件我来拼。")
    elif not show_fields:
        lines.append("这不是字段口径的问题，是 SQL 没跑通，我已经自动重试过了。")
    return "\n".join(lines)


