from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
import zlib
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .config import settings


DB_PATH = settings.data_dir / "agent.db"


def _secret_bytes() -> bytes:
    if settings.app_secret_key:
        return hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest()
    key_file = settings.data_dir / ".secret.key"
    if key_file.exists():
        return base64.urlsafe_b64decode(key_file.read_bytes())
    raw = hashlib.sha256(secrets.token_bytes(64)).digest()
    key_file.write_bytes(base64.urlsafe_b64encode(raw))
    return raw


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_secret_bytes()))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_enc TEXT NOT NULL,
                model TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS result_cache (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS query_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                intent TEXT,
                sql_text TEXT,
                result_id TEXT,
                summary TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_memory (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_sync (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_name TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                ragflow_doc_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(dataset_name, relative_path, chunk_index)
            );

            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                intent TEXT,
                result_id TEXT,
                datasets TEXT,
                references_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS example_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                sql_text TEXT NOT NULL,
                doc_name TEXT NOT NULL,
                trusted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thinking TEXT,
                references_json TEXT,
                result_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sql_error_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                issue_type TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                fix_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        # 轻量迁移：示例表增加 normalized_sql 列（带占位符、可换参数重放的版本）。
        # 不动 sql_text / doc_name：doc_name 是示例的主键（RAGFlow 文档名、看板、去重都靠它），
        # 改了 sql_text 名字就得跟着变，会牵动一大片；所以归一化版本单独放一列。
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(example_pairs)")}
        if "normalized_sql" not in columns:
            conn.execute("ALTER TABLE example_pairs ADD COLUMN normalized_sql TEXT")
        # 老库补列（幂等）
        for statement in (
            "ALTER TABLE conversation_messages ADD COLUMN result_id TEXT",
            # 三元组：问题 + 查询意图(JSON) + SQL。老库补列，新库建表后同样执行也不会报错。
            "ALTER TABLE example_pairs ADD COLUMN spec_json TEXT",
            # 固定看板：卡片就是示例库里的条目（approve 即入池），这几个列管看板自己的状态
            "ALTER TABLE example_pairs ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE example_pairs ADD COLUMN board INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE example_pairs ADD COLUMN group_name TEXT NOT NULL DEFAULT '默认'",
            "ALTER TABLE example_pairs ADD COLUMN last_summary TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE example_pairs ADD COLUMN last_run_at TEXT",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        # 老库里可能存着带 `/` 的会话 id（拼进 URL 后路由永远 404，见 safe_conversation_id）。
        # 一次性归一这些 id 并把消息搬过去；目标 id 已存在就直接合并。
        for bad_id, in conn.execute("SELECT id FROM conversations").fetchall():
            fixed = safe_conversation_id(bad_id)
            if fixed == bad_id:
                continue
            if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (fixed,)).fetchone():
                conn.execute("DELETE FROM conversations WHERE id = ?", (bad_id,))
            else:
                conn.execute("UPDATE conversations SET id = ? WHERE id = ?", (fixed, bad_id))
            conn.execute(
                "UPDATE conversation_messages SET conversation_id = ? WHERE conversation_id = ?",
                (fixed, bad_id),
            )


def _now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _cutoff(days: float) -> str:
    """保留期截止时间（带时区的 ISO 串，和表里存的时间同格式，直接字符串比较）。"""
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="milliseconds")


def purge_old_records(
    log_days: int | None = None,
    log_max_rows: int | None = None,
    history_days: int | None = None,
    retrieval_days: int | None = None,
) -> dict[str, int]:
    """按保留期清理本地库里的旧记录（日志表 / 历史表 / 检索日志）。

    `result_cache` 有自己的策略（TTL 天数 + 条数上限，见 save_result），这里不重复管。
    实测本地 agent.db 涨到 209MB，大头就是 agent_logs（8 万行）和 query_history（1.1 万行）；
    删完要配合 vacuum_database() 才能真正把文件缩回去（SQLite 删行不还盘）。
    """
    days = int(log_days if log_days is not None else settings.log_retention_days)
    rows_cap = int(log_max_rows if log_max_rows is not None else settings.log_max_rows)
    history = int(history_days if history_days is not None else settings.history_retention_days)
    retrieval = int(
        retrieval_days if retrieval_days is not None else settings.retrieval_retention_days
    )
    deleted: dict[str, int] = {}
    with get_connection() as conn:
        deleted["agent_logs"] = conn.execute(
            "DELETE FROM agent_logs WHERE time < ?", (_cutoff(days),)
        ).rowcount
        if rows_cap > 0:
            # 再按最大行数兜一层：日志写得快时，光靠天数也能长到很大
            deleted["agent_logs_capped"] = conn.execute(
                "DELETE FROM agent_logs WHERE id NOT IN "
                "(SELECT id FROM agent_logs ORDER BY id DESC LIMIT ?)",
                (rows_cap,),
            ).rowcount
        deleted["query_history"] = conn.execute(
            "DELETE FROM query_history WHERE created_at < ?", (_cutoff(history),)
        ).rowcount
        deleted["retrieval_log"] = conn.execute(
            "DELETE FROM retrieval_log WHERE created_at < ?", (_cutoff(retrieval),)
        ).rowcount
    return deleted


def database_size_bytes() -> int:
    """本地库文件大小（含 WAL）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            total += path.stat().st_size
    return total


def vacuum_database() -> bool:
    """回收文件空间（SQLite 删行不还盘，必须 VACUUM）。失败只记日志，不抛。"""
    from .services.logging_service import log_error, log_event

    try:
        # 不能用 get_connection()：VACUUM 不能在事务里跑
        conn = sqlite3.connect(DB_PATH, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log_error("store", "vacuum_failed", exc)
        return False
    log_event("store", "INFO", "vacuum_done", size_bytes=database_size_bytes())
    return True


def list_model_configs() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, base_url, api_key_enc, model, is_default FROM model_configs ORDER BY created_at"
        ).fetchall()
    fernet = _fernet()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "base_url": row["base_url"],
            "api_key": fernet.decrypt(row["api_key_enc"].encode("utf-8")).decode("utf-8"),
            "model": row["model"],
            "is_default": bool(row["is_default"]),
        }
        for row in rows
    ]


def get_default_model() -> dict[str, Any] | None:
    models = list_model_configs()
    for model in models:
        if model["is_default"]:
            return model
    if models:
        return models[0]
    if settings.qwen_api_key:
        return {
            "id": "qwen-default",
            "name": "Qwen 默认",
            "base_url": settings.qwen_base_url,
            "api_key": settings.qwen_api_key,
            "model": settings.qwen_model,
            "is_default": True,
        }
    return None


def save_model_config(model: dict[str, Any]) -> dict[str, Any]:
    model_id = str(uuid.uuid4())
    fernet = _fernet()
    encrypted = fernet.encrypt(model["api_key"].encode("utf-8")).decode("utf-8")
    with get_connection() as conn:
        if model.get("is_default"):
            conn.execute("UPDATE model_configs SET is_default=0")
        conn.execute(
            """
            INSERT INTO model_configs(id, name, base_url, api_key_enc, model, is_default, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_id,
                model["name"],
                model["base_url"],
                encrypted,
                model["model"],
                1 if model.get("is_default") else 0,
                _now(),
            ),
        )
    return {
        "id": model_id,
        "name": model["name"],
        "base_url": model["base_url"],
        "model": model["model"],
        "is_default": bool(model.get("is_default")),
    }


def delete_model_config(model_id: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM model_configs WHERE id = ?", (model_id,))


def save_result(result_id: str, payload: dict[str, Any]) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO result_cache(id, payload, created_at) VALUES (?, ?, ?)",
            (result_id, _encode_payload(payload), _now()),
        )
        # 保留策略：最近 N 条 + TTL 天数（两条线都要满足，避免本地库无限增长）。
        # 以前只看条数（200 条），问一会儿就把早上的结果挤掉，历史里点"查看这次结果"就 404。
        from datetime import datetime, timedelta

        cutoff = (datetime.now().astimezone() - timedelta(days=settings.result_cache_ttl_days)).isoformat(
            timespec="seconds"
        )
        conn.execute("DELETE FROM result_cache WHERE created_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM result_cache WHERE rowid NOT IN "
            "(SELECT rowid FROM result_cache ORDER BY rowid DESC LIMIT ?)",
            (settings.result_cache_limit,),
        )


def get_result(result_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT payload FROM result_cache WHERE id = ?", (result_id,)
        ).fetchone()
    return _decode_payload(row["payload"]) if row else None


# 结果缓存里存的是整份结果（含 500 行明细），实测平均 63KB、最大 870KB、合计 116MB，
# 是本地库最大的那块。JSON 文本压缩比很好（8%~38%），压完再存，读写transparent。
_PAYLOAD_PREFIX = "z:"


def _encode_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    blob = zlib.compress(raw, 6)
    if len(blob) >= len(raw):
        return raw.decode("utf-8")
    return _PAYLOAD_PREFIX + base64.b64encode(blob).decode("ascii")


def _decode_payload(text: str) -> dict[str, Any]:
    text = str(text or "")
    if text.startswith(_PAYLOAD_PREFIX):
        raw = zlib.decompress(base64.b64decode(text[len(_PAYLOAD_PREFIX) :]))
        return json.loads(raw.decode("utf-8"))
    return json.loads(text)


def compact_result_cache() -> dict[str, int]:
    """把历史遗留的未压缩结果缓存就地压缩（一次性迁移），返回 {扫描, 压缩}。"""
    scanned = compacted = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT id, payload FROM result_cache").fetchall()
        for row in rows:
            scanned += 1
            payload = str(row["payload"] or "")
            if payload.startswith(_PAYLOAD_PREFIX):
                continue
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                continue
            encoded = _encode_payload(data)
            if len(encoded) < len(payload):
                conn.execute(
                    "UPDATE result_cache SET payload = ? WHERE id = ?", (encoded, row["id"])
                )
                compacted += 1
    return {"scanned": scanned, "compacted": compacted}


def add_history(
    question: str,
    intent: str | None = None,
    sql_text: str | None = None,
    result_id: str | None = None,
    summary: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO query_history(question, intent, sql_text, result_id, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (question, intent, sql_text, result_id, summary, _now()),
        )


def add_sql_error(
    question: str,
    keywords: str,
    issue_type: str,
    reason: str,
    fix_note: str = "",
) -> None:
    """记一条"这次错在哪"的经验（docs/项目智能化升级方案.md 升级5 错误记忆）。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sql_error_memory(question, keywords, issue_type, reason, fix_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (question[:300], keywords[:200], issue_type[:40], reason[:300], fix_note[:300], _now()),
        )


def save_user_feedback(
    question: str,
    sql_text: str = "",
    reply: str = "",
    comment: str = "",
    kind: str = "bad",
    template_id: str = "",
    context: str = "",
) -> None:
    """用户"答错了"的反馈入库（方案第四节：bad case 闭环，不追求自动修复）。

    复用 sql_error_memory 表：question=原始问题，reason=用户原话，
    issue_type=bad/good，fix_note 存 SQL 片段，方便每周翻一遍批量改提示词/MDL。

    template_id：这次用的是哪条模板/示例（"连续 3 次答错自动降级"要靠它归因，
    docs/模板沉淀标准与复用检查_系统性方案.md §5.2）。存在 keywords 列（以前没用上）。
    context：自动收集的上下文（plan 条件 / 模板条件 / 差异），不要求用户输入。
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sql_error_memory(question, keywords, issue_type, reason, fix_note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                question[:300],
                str(template_id)[:200],
                str(kind)[:20],
                (str(comment)[:200] + (" || " + str(context)[:400] if context else ""))[:600],
                (str(reply)[:150] + " || " + str(sql_text)[:150])[:300],
                _now(),
            ),
        )


def recent_bad_feedback(template_id: str, window: int = 3) -> list[str]:
    """这条模板/示例最近 window 次的反馈类型（新→旧）。用来判"连续 3 次答错"。"""
    if not str(template_id or ""):
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT issue_type FROM sql_error_memory WHERE keywords = ? "
            "AND issue_type IN ('bad','good') ORDER BY id DESC LIMIT ?",
            (str(template_id), int(window)),
        ).fetchall()
    return [str(row["issue_type"]) for row in rows]


def list_sql_errors(limit: int = 80) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT question, keywords, issue_type, reason, fix_note, created_at
            FROM sql_error_memory ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_history(limit: int = 20) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT question, intent, sql_text, result_id, summary, created_at
            FROM query_history ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]



_FAVORITES_KEY = "favorites"


def list_favorites() -> list[dict[str, Any]]:
    value = get_memory(_FAVORITES_KEY)
    return value if isinstance(value, list) else []


def save_favorite(question: str) -> list[dict[str, Any]]:
    """收藏一个问法（重复只留一条，最新的排前面）。"""
    text = str(question or "").strip()
    if not text:
        return list_favorites()
    items = [item for item in list_favorites() if str(item.get("question") or "") != text]
    items.insert(0, {"question": text, "created_at": _now()})
    set_memory(_FAVORITES_KEY, items[:50])
    return items[:50]


def delete_favorite(question: str) -> list[dict[str, Any]]:
    text = str(question or "").strip()
    items = [item for item in list_favorites() if str(item.get("question") or "") != text]
    set_memory(_FAVORITES_KEY, items)
    return items


def set_memory(key: str, value: Any) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO user_memory(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False, default=str), _now()),
        )


def get_memory(key: str) -> Any | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM user_memory WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def delete_memory(key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_memory WHERE key = ?", (key,))


def safe_conversation_id(conversation_id: str) -> str:
    """会话 id 会拼进 URL（`/api/conversations/{id}`）与前端路由。

    带 `/` 的 id（例如按「供应商/模型」拼出来的测试 id）即使经过
    encodeURIComponent，uvicorn 也会先把 `%2F` 解码回 `/` 再路由，导致
    路由匹配不上、详情接口永远 404。这里统一把不安全字符换成 `-`
    （正常 UUID 会话不受影响）。
    """
    return re.sub(r"[^0-9A-Za-z_.\-]", "-", str(conversation_id or "")) or "default"


def ensure_conversation(conversation_id: str, title: str = "") -> None:
    conversation_id = safe_conversation_id(conversation_id)
    now = _now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, title, now, now),
            )
        elif title and not (row["title"] or "").strip():
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
        else:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )


def add_conversation_message(
    conversation_id: str,
    role: str,
    content: str,
    thinking: str = "",
    references: list[dict[str, Any]] | None = None,
    result_id: str | None = None,
) -> None:
    conversation_id = safe_conversation_id(conversation_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO conversation_messages(
                conversation_id, role, content, thinking, references_json, result_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                thinking or "",
                json.dumps(references or [], ensure_ascii=False, default=str),
                result_id,
                _now(),
            ),
        )


def list_conversations(limit: int = 50) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM conversation_messages m
                     WHERE m.conversation_id = c.id) AS message_count
            FROM conversations c
            ORDER BY c.updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_conversation_messages(conversation_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT role, content, thinking, references_json, result_id, created_at
            FROM conversation_messages WHERE conversation_id = ?
            ORDER BY id ASC LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    messages: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["references"] = json.loads(item.pop("references_json") or "[]")
        messages.append(item)
    return messages


def delete_conversation(conversation_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_id = ?", (conversation_id,)
        )
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def all_conversation_ids() -> list[str]:
    """所有对话 id（"清空全部"用）。"""
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM conversations").fetchall()
    return [str(row["id"]) for row in rows]


def delete_conversations(conversation_ids: list[str]) -> int:
    """批量删除对话（含消息）。返回真正删掉的对话条数。"""
    ids = [str(item) for item in conversation_ids if item]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with get_connection() as conn:
        conn.execute(
            f"DELETE FROM conversation_messages WHERE conversation_id IN ({marks})", ids
        )
        cursor = conn.execute(f"DELETE FROM conversations WHERE id IN ({marks})", ids)
    return int(cursor.rowcount or 0)


def list_sync_rows(dataset_name: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT relative_path, content_hash, chunk_index, ragflow_doc_id
            FROM knowledge_sync WHERE dataset_name = ?
            """,
            (dataset_name,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_sync_row(
    dataset_name: str,
    relative_path: str,
    content_hash: str,
    chunk_index: int,
    ragflow_doc_id: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_sync(
                dataset_name, relative_path, content_hash, chunk_index, ragflow_doc_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_name, relative_path, chunk_index)
            DO UPDATE SET content_hash=excluded.content_hash,
                          ragflow_doc_id=excluded.ragflow_doc_id,
                          updated_at=excluded.updated_at
            """,
            (dataset_name, relative_path, content_hash, chunk_index, ragflow_doc_id, _now()),
        )


def delete_sync_rows(dataset_name: str, relative_path: str) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ragflow_doc_id FROM knowledge_sync WHERE dataset_name = ? AND relative_path = ?",
            (dataset_name, relative_path),
        ).fetchall()
        doc_ids = [row["ragflow_doc_id"] for row in rows]
        conn.execute(
            "DELETE FROM knowledge_sync WHERE dataset_name = ? AND relative_path = ?",
            (dataset_name, relative_path),
        )
    return doc_ids


def clear_sync_rows(dataset_name: str) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ragflow_doc_id FROM knowledge_sync WHERE dataset_name = ?",
            (dataset_name,),
        ).fetchall()
        doc_ids = [row["ragflow_doc_id"] for row in rows]
        conn.execute("DELETE FROM knowledge_sync WHERE dataset_name = ?", (dataset_name,))
    return doc_ids


def add_retrieval_log(
    question: str,
    intent: str | None,
    result_id: str | None,
    datasets: list[str],
    references: list[dict[str, Any]],
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO retrieval_log(question, intent, result_id, datasets, references_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                intent,
                result_id,
                json.dumps(datasets, ensure_ascii=False),
                json.dumps(references, ensure_ascii=False, default=str),
                _now(),
            ),
        )
        conn.execute(
            "DELETE FROM retrieval_log WHERE id NOT IN "
            "(SELECT id FROM retrieval_log ORDER BY id DESC LIMIT 100)"
        )


def list_retrieval_logs(limit: int = 20) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, question, intent, result_id, datasets, references_json, created_at
            FROM retrieval_log ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "question": row["question"],
            "intent": row["intent"],
            "result_id": row["result_id"],
            "datasets": json.loads(row["datasets"]) if row["datasets"] else [],
            "references": json.loads(row["references_json"]) if row["references_json"] else [],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def add_example_pair(
    question: str,
    sql_text: str,
    doc_name: str,
    trusted: bool,
    spec_json: str | None = None,
    normalized_sql: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO example_pairs(question, sql_text, doc_name, trusted, spec_json,
                                      normalized_sql, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                sql_text,
                doc_name,
                1 if trusted else 0,
                spec_json,
                normalized_sql,
                _now(),
            ),
        )


def update_example_normalized_sql(doc_name: str, normalized_sql: str) -> bool:
    """补写示例的可重放 SQL（带占位符版本）。"""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE example_pairs SET normalized_sql = ? WHERE doc_name = ?",
            (normalized_sql, doc_name),
        )
        return bool(cursor.rowcount)


def list_recorded_example_pairs(limit: int = 500) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, question, sql_text, doc_name, trusted, spec_json, normalized_sql,
                   created_at
            FROM example_pairs ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_example_pairs(doc_names: list[str]) -> int:
    names = [name for name in doc_names if name]
    if not names:
        return 0
    marks = ",".join("?" for _ in names)
    with get_connection() as conn:
        cursor = conn.execute(
            f"DELETE FROM example_pairs WHERE doc_name IN ({marks})", names
        )
        return cursor.rowcount


def list_board_examples(limit: int = 200) -> list[dict[str, Any]]:
    """固定看板的卡片：示例库里所有**带 SQL** 的问答对（置顶优先）。

    不区分"系统自带 / 我固化的"：RAGFlow 示例库已有的问答对和用户 approve 进来的
    是同一种东西，都该在看板上能点、能刷新（docs/固定看板重构与收藏固化方案.md 第二、七章）。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, question, sql_text, doc_name, trusted, spec_json, normalized_sql,
                   pinned, board, group_name, last_summary, last_run_at, created_at
            FROM example_pairs
            WHERE TRIM(COALESCE(sql_text, '')) <> ''
               OR COALESCE(spec_json, '') LIKE '%subtasks_summary%'
            ORDER BY pinned DESC, COALESCE(last_run_at, created_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_example_by_doc_name(doc_name: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, question, sql_text, doc_name, trusted, spec_json, normalized_sql,
                   pinned, board, group_name, last_summary, last_run_at, created_at
            FROM example_pairs WHERE doc_name = ? ORDER BY id DESC LIMIT 1
            """,
            (doc_name,),
        ).fetchone()
    return dict(row) if row else None


def mark_example_trusted(doc_name: str) -> bool:
    """把已有示例升级成"已确认"（重复 approve 时用，避免老条目一直是未确认）。"""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE example_pairs SET trusted = 1 WHERE doc_name = ?", (doc_name,)
        )
        return bool(cursor.rowcount)


def mark_examples_trusted(doc_names: list[str]) -> int:
    """批量把已有示例标成"已确认"，返回实际改动的条数（RAGFlow 对账回填用）。"""
    names = [name for name in doc_names if name]
    if not names:
        return 0
    marks = ",".join("?" for _ in names)
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE example_pairs SET trusted = 1 "
            f"WHERE trusted = 0 AND doc_name IN ({marks})",
            names,
        )
        return int(cursor.rowcount or 0)


def update_example_spec(doc_name: str, spec_json: str) -> bool:
    """补写示例的结构 spec（老条目 approve 时没存 spec，看板/复用就判不了同型）。"""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE example_pairs SET spec_json = ? WHERE doc_name = ? AND COALESCE(spec_json, '') = ''",
            (spec_json, doc_name),
        )
        return bool(cursor.rowcount)


def set_example_spec(doc_name: str, spec_json: str) -> bool:
    """**覆盖**示例的 spec（`update_example_spec` 只在空值时写，修不了已有的错 spec）。"""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE example_pairs SET spec_json = ? WHERE doc_name = ?",
            (spec_json, doc_name),
        )
        return bool(cursor.rowcount)


def untrust_examples(doc_names: list[str]) -> int:
    """把示例标成"未确认"（回填会错的示例宁可退回 Wren，也不当确定性口径喂给用户）。"""
    names = [name for name in doc_names if name]
    if not names:
        return 0
    marks = ",".join("?" for _ in names)
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE example_pairs SET trusted = 0 WHERE doc_name IN ({marks})", names
        )
        return int(cursor.rowcount or 0)


def set_example_spec(doc_name: str, spec_json: str) -> bool:
    """**覆盖**写示例的 spec（修数据用：补 `display_question`、清长对话补全留下的问法痕迹）。

    与 `update_example_spec`（只在空时补写）不同，这里是有意覆盖——修完要能立刻生效。
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE example_pairs SET spec_json = ? WHERE doc_name = ?",
            (spec_json, doc_name),
        )
        return bool(cursor.rowcount)


def append_example_variant(doc_name: str, question: str, limit: int = 8) -> bool:
    """把"同一题的不同问法"记到已有示例上（不再新存一条）。

    见 docs/固定看板重构与收藏固化方案.md 第七章 §9：结构同型、槽位相同的问法
    合并成问法变体，而不是无限新增，控制召回噪声。
    """
    text = str(question or "").strip()
    if not doc_name or not text:
        return False
    with get_connection() as conn:
        row = conn.execute(
            "SELECT spec_json FROM example_pairs WHERE doc_name = ? LIMIT 1", (doc_name,)
        ).fetchone()
        if not row:
            return False
        spec: dict[str, Any] = {}
        try:
            spec = json.loads(str(row["spec_json"] or "{}") or "{}")
        except Exception:  # noqa: BLE001
            spec = {}
        variants = [str(item) for item in (spec.get("variants") or []) if str(item).strip()]
        if text in variants:
            return False
        variants.append(text)
        spec["variants"] = variants[-limit:]
        conn.execute(
            "UPDATE example_pairs SET spec_json = ? WHERE doc_name = ?",
            (json.dumps(spec, ensure_ascii=False), doc_name),
        )
        return True


def update_board_card(
    doc_name: str,
    pinned: bool | None = None,
    board: bool | None = None,
    group_name: str | None = None,
    last_summary: str | None = None,
    touch_run: bool = False,
) -> bool:
    """改看板卡片状态（置顶/移出/分组/最近一次摘要）。"""
    sets: list[str] = []
    values: list[Any] = []
    if pinned is not None:
        sets.append("pinned = ?")
        values.append(1 if pinned else 0)
    if board is not None:
        sets.append("board = ?")
        values.append(1 if board else 0)
    if group_name is not None:
        sets.append("group_name = ?")
        values.append(group_name)
    if last_summary is not None:
        sets.append("last_summary = ?")
        values.append(last_summary)
    if touch_run:
        sets.append("last_run_at = ?")
        values.append(_now())
    if not sets:
        return False
    values.append(doc_name)
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE example_pairs SET {', '.join(sets)} WHERE doc_name = ?", values
        )
        return bool(cursor.rowcount)


init_db()
