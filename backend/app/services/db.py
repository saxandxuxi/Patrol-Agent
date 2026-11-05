from __future__ import annotations

import csv
import base64
import hashlib
import io
import json
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Any

import paramiko

from ..config import settings
from .logging_service import log_event

# 图片缓存要用的后缀 → content-type（只缓存认得出类型的图，避免把别的文件当图片发出去）
_IMAGE_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


_PG_ALIAS_QUOTED = re.compile(r"(?i)\bAS\s*'([^']+)'")
_PG_BACKTICK = re.compile(r"`([^`]+)`")


def pg_dialect(sql: str) -> str:
    """把 MySQL 写法的方言换成 PostgreSQL 能跑的（只动别名/标识符，不碰字符串字面值）。

    实测问题：快捷查询选 PG 时，SQL 是 MySQL 写法 `SELECT m.create_time AS '创建时间', …`，
    PG 里单引号是**字符串字面值**、不能当别名 → `syntax error at or near "'创建时间'"`。同理
    MySQL 的反引号标识符在 PG 里也要换成双引号。这里做最小替换：
      · `AS '别名'` → `AS "别名"`
      · `` `列名` `` → `"列名"`
    只改这两类（别名/标识符位置），WHERE 里的 `'值'` 一律不动。
    """
    text = str(sql or "")
    if not text:
        return text
    text = _PG_ALIAS_QUOTED.sub(lambda match: f'AS "{match.group(1)}"', text)
    return _PG_BACKTICK.sub(lambda match: f'"{match.group(1)}"', text)


class DatabaseError(RuntimeError):
    pass


class DatabaseManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: paramiko.SSHClient | None = None
        self._seaweedfs_prefix: str | None = None
        self._seaweedfs_buckets: list[str] | None = None
        # 批量探测时记住每个对象命中的候选地址（批量取图直接用它，一条 curl 就够）
        self._seaweedfs_resolved: dict[str, str] = {}
        self._image_cache_pruned_at = 0.0

    # ── 图片本地缓存 ────────────────────────────────────────────────────────────
    # 图都在 SSH 隧道后面的 SeaweedFS 上，每看一次图就穿一次隧道。工作台回看、看板刷新、
    # 同一批图重复打开都走缓存，别每次都去拉（实测单张 0.16~0.47 秒，42 张就是十几秒）。
    def _image_cache_file(self, path: str) -> Path | None:
        suffix = Path(str(path or "")).suffix.lower()
        if suffix not in _IMAGE_CONTENT_TYPES:
            return None  # 类型不认识的（.bin/.json 之类）不缓存，免得当图片发出去
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
        return settings.data_dir / "image_cache" / f"{digest}{suffix}"

    def _cached_image(self, path: str) -> tuple[str, bytes] | None:
        file = self._image_cache_file(path)
        if not file or not file.exists():
            return None
        ttl_days = float(getattr(settings, "image_cache_ttl_days", 7) or 7)
        try:
            if ttl_days > 0 and time.time() - file.stat().st_mtime > ttl_days * 86400:
                return None
            return _IMAGE_CONTENT_TYPES[file.suffix.lower()], file.read_bytes()
        except OSError:
            return None

    def _cached_image_ok(self, path: str) -> bool:
        """缓存里有没有这张图（**只 stat 不读正文**：批量判存在时读几十 MB 没必要）。

        口径必须和 `_cached_image` 一致（含 TTL），否则会出现"回答里有这张图、
        `/api/image` 却 404"的自相矛盾。
        """
        file = self._image_cache_file(path)
        if not file or not file.exists():
            return False
        ttl_days = float(getattr(settings, "image_cache_ttl_days", 7) or 7)
        if ttl_days <= 0:
            return True
        try:
            return time.time() - file.stat().st_mtime <= ttl_days * 86400
        except OSError:
            return False

    def _store_image(self, path: str, body: bytes) -> None:
        file = self._image_cache_file(path)
        if not file or not body:
            return
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body)
            self._prune_image_cache(file.parent)
        except OSError:
            pass

    def _prune_image_cache(self, directory: Path) -> None:
        """清过期缓存 + 控制总大小；最多 10 分钟做一次，别每次写文件都全目录扫。

        本地库 disk 也是用户的机器：样图单张就有 1MB 量级（实测 45 张 45MB），
        不设上限的话看到几百张就会堆到几百 MB。
        """
        now = time.time()
        if now - self._image_cache_pruned_at < 600:
            return
        self._image_cache_pruned_at = now
        ttl_days = float(getattr(settings, "image_cache_ttl_days", 7) or 7)
        max_mb = float(getattr(settings, "image_cache_max_mb", 500) or 500)
        cutoff = now - ttl_days * 86400 if ttl_days > 0 else now
        try:
            entries = [item for item in directory.iterdir() if item.is_file()]
            kept: list[tuple[float, int, Path]] = []
            for item in entries:
                stat = item.stat()
                if stat.st_mtime < cutoff:
                    item.unlink()
                    continue
                kept.append((stat.st_mtime, stat.st_size, item))
            total = sum(size for _mtime, size, _item in kept)
            if max_mb > 0 and total > max_mb * 1048576:
                for _mtime, size, item in sorted(kept):  # 从最旧的开始删
                    if total <= max_mb * 1048576:
                        break
                    item.unlink()
                    total -= size
        except OSError:
            pass

    def _ssh(self) -> paramiko.SSHClient:
        with self._lock:
            if self._client and self._client.get_transport() and self._client.get_transport().is_active():
                return self._client
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=settings.ssh_host,
                port=settings.ssh_port,
                username=settings.ssh_user,
                password=settings.ssh_password,
                timeout=8,
                banner_timeout=8,
                auth_timeout=8,
                look_for_keys=False,
                allow_agent=False,
            )
            self._client = client
            return client

    def _run(self, command: str, timeout: int = 60) -> bytes:
        with self._lock:
            client = self._ssh()
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read()
            err = stderr.read()
            status = stdout.channel.recv_exit_status()
        if status != 0:
            message = err.decode("utf-8", errors="replace").strip()
            raise DatabaseError(message or f"远程命令失败，退出码 {status}")
        return out

    def _mysql_command(self, sql: str) -> str:
        password = shlex.quote(settings.mysql_password)
        user = shlex.quote(settings.mysql_user)
        database = shlex.quote(settings.mysql_database)
        query = shlex.quote(sql)
        return (
            f"docker exec -e MYSQL_PWD={password} {settings.mysql_container} mysql "
            f"-h127.0.0.1 -P{settings.mysql_port} -u{user} -D {database} "
            f"--default-character-set={settings.mysql_charset} "
            f"--batch --raw -e {query}"
        )

    def _pg_command(self, sql: str) -> str:
        password = shlex.quote(settings.pg_password)
        user = shlex.quote(settings.pg_user)
        database = shlex.quote(settings.pg_database)
        query = shlex.quote(sql)
        return (
            f"docker exec -e PGPASSWORD={password} {settings.pg_container} psql "
            f"-h127.0.0.1 -p{settings.pg_port} -U {user} -d {database} "
            f"--csv -c {query}"
        )

    def _parse_mysql(self, output: bytes) -> tuple[list[str], list[dict[str, Any]]]:
        text = output.decode("utf-8", errors="replace").replace("\r\n", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            return [], []
        columns = lines[0].split("\t")
        rows = []
        for line in lines[1:]:
            values = line.split("\t")
            values += [""] * (len(columns) - len(values))
            rows.append(dict(zip(columns, values)))
        return columns, rows

    def _parse_pg(self, output: bytes) -> tuple[list[str], list[dict[str, Any]]]:
        text = output.decode("utf-8", errors="replace")
        records = list(csv.reader(io.StringIO(text)))
        if not records:
            return [], []
        columns = records[0]
        rows = [dict(zip(columns, row)) for row in records[1:] if row]
        return columns, rows

    def query(self, kind: str, sql: str) -> dict[str, Any]:
        if kind == "mysql":
            columns, rows = self._parse_mysql(self._run(self._mysql_command(sql)))
        elif kind == "postgresql":
            # 方言归一：库里存的/模型写的是 MySQL 写法，直接丢给 PG 会挂在别名上
            # （实测快捷查询选 PG：`SELECT m.create_time AS '创建时间' …` →
            #  PG 里单引号是字符串字面值，不能当别名 → syntax error at or near "'创建时间'"）。
            fixed = pg_dialect(sql)
            if fixed != sql:
                sql = fixed  # 回给上层的是**实际执行**的 SQL（前端"查看执行 SQL"看到的就是它）
                log_event(
                    "db",
                    "INFO",
                    "pg_dialect_normalized",
                    question=str(sql)[:60],
                )
            columns, rows = self._parse_pg(self._run(self._pg_command(fixed)))
        else:
            raise DatabaseError(f"不支持的数据库类型：{kind}")
        return {
            "columns": columns,
            "rows": rows,
            "source": kind,
            "sql": sql,
        }

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mysql": "unknown", "postgresql": "unknown"}
        for kind in ("mysql", "postgresql"):
            try:
                self.query(kind, "SELECT 1 AS ok")
                result[kind] = "ok"
            except Exception as exc:
                result[kind] = str(exc)
        return result

    def _curl_file(self, url: str) -> tuple[int, str, bytes]:
        output = self._run(
            f"curl -sS -D - --max-time 30 {shlex.quote(url)}",
            timeout=40,
        )
        header, _, body = output.partition(b"\r\n\r\n")
        lines = header.decode("latin1", errors="replace").splitlines()
        status = 0
        content_type = "application/octet-stream"
        if lines:
            parts = lines[0].split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        for line in lines[1:]:
            if line.lower().startswith("content-type:"):
                content_type = line.split(":", 1)[1].strip()
                break
        return status, content_type, body

    def _buckets(self) -> list[str]:
        """列出 SeaweedFS 的桶（排除 .system 等内部桶）。"""
        if self._seaweedfs_buckets is None:
            self._seaweedfs_buckets = []
            try:
                output = self._run(
                    "curl -sS --max-time 20 -H 'Accept: application/json' "
                    f"{settings.seaweedfs_base_url}/buckets/?pretty=y",
                    timeout=30,
                )
                data = json.loads(output.decode("utf-8", errors="replace") or "{}")
                names = [
                    str(entry.get("FullPath", "")).rstrip("/").split("/")[-1]
                    for entry in (data.get("Entries") or [])
                ]
                self._seaweedfs_buckets = [n for n in names if n and not n.startswith(".")]
            except Exception:
                self._seaweedfs_buckets = []
        return self._seaweedfs_buckets

    def _seaweedfs_candidates(self, clean: str) -> list[tuple[str, str]]:
        """候选 (前缀, 完整路径)：已命中前缀 → 配置桶 → 原始路径 → 自动发现的桶。"""
        candidates: list[tuple[str, str]] = []
        # 库里两种存法，第二种以前解析不出来（永远 404）：
        #   1) 对象键：`102367594567/2026/07/30/xxx.jpeg` → 补桶前缀就行；
        #   2) 应用地址：`/base/sys-file/<桶>/<文件名>`（路段/路线封面 picture_cover）——
        #      去掉 `base/sys-file/` 之后本身就是「桶/对象键」，不能再叠一层桶前缀。
        #      实测 basic_facilities_sections.picture_cover 的这张 png：对象确实在
        #      buckets/airoad-bj-1321614036/ 下（162KB），但按老拼法拼成
        #      buckets/<桶>/base/sys-file/<桶>/<文件> → 404，用户看到"图片调不出来"。
        if clean.startswith("base/sys-file/"):
            bucket, _, key = clean[len("base/sys-file/") :].partition("/")
            if bucket and key:
                prefix = f"buckets/{bucket}/"
                candidates.append((prefix, f"{prefix}{key}"))
        if self._seaweedfs_prefix is not None:
            candidates.append((self._seaweedfs_prefix, f"{self._seaweedfs_prefix}{clean}"))
        if settings.seaweedfs_bucket:
            prefix = f"buckets/{settings.seaweedfs_bucket}/"
            candidates.append((prefix, f"{prefix}{clean}"))
        candidates.append(("", clean))
        for bucket in self._buckets():
            if bucket == settings.seaweedfs_bucket:
                continue
            prefix = f"buckets/{bucket}/"
            candidates.append((prefix, f"{prefix}{clean}"))
        return candidates

    def fetch_seaweedfs(self, path: str) -> tuple[int, str, bytes]:
        """按候选前缀依次取文件。

        库里存的是 `设备号/日期/uuid.jpeg` 这类对象键，而 SeaweedFS 用 S3 桶组织，
        真实地址是 `/buckets/<bucket>/<对象键>`；这里自动补桶前缀并记住命中的前缀。
        """
        clean = path.lstrip("/")
        cached = self._cached_image(clean)
        if cached:
            content_type, body = cached
            return 200, content_type, body
        # 之前探测/取图已经解析过的对象，直接用命中的那个地址（省一轮前缀试错）
        resolved = self._seaweedfs_resolved.get(clean)
        if resolved:
            status, content_type, body = self._curl_file(
                f"{settings.seaweedfs_base_url}/{resolved}"
            )
            if status == 200 and body:
                self._store_image(clean, body)
                return status, content_type, body
            self._seaweedfs_resolved.pop(clean, None)
        last: tuple[int, str, bytes] = (404, "application/octet-stream", b"")
        for prefix, candidate in self._seaweedfs_candidates(clean):
            status, content_type, body = self._curl_file(f"{settings.seaweedfs_base_url}/{candidate}")
            if status == 200 and body:
                self._seaweedfs_prefix = prefix
                self._remember_resolved(clean, candidate)
                self._store_image(clean, body)
                return status, content_type, body
            last = (status, content_type, body)
        return last

    def _remember_resolved(self, path: str, candidate: str) -> None:
        """记住"这个对象用哪个地址取到的"（上限 1000 条，多了丢最早的）。"""
        if len(self._seaweedfs_resolved) >= 1000:
            for key in list(self._seaweedfs_resolved)[:200]:
                self._seaweedfs_resolved.pop(key, None)
        self._seaweedfs_resolved[path] = candidate

    def fetch_seaweedfs_many(self, paths: list[str]) -> dict[str, tuple[int, str, bytes]]:
        """**一条远程命令**批量取图，返回 {path: (status, content_type, bytes)}。

        一张图一次 SSH 往返（`_run` 串行加锁），42 张就是十几秒。这里把一批图在远端
        `curl | base64` 打成一个流取回来、本地再切开——总共一次往返。命中本地缓存的直接返回，
        连远端都不去。
        """
        out: dict[str, tuple[int, str, bytes]] = {}
        pending: list[str] = []
        for raw in paths:
            path = str(raw or "").lstrip("/")
            if not path or path in out or path in pending:
                continue
            cached = self._cached_image(path)
            if cached:
                content_type, body = cached
                out[path] = (200, content_type, body)
            else:
                pending.append(path)
        if not pending:
            return out

        parts: list[str] = []
        candidates_by_index: dict[int, list[str]] = {}
        for index, path in enumerate(pending):
            candidate = self._seaweedfs_resolved.get(path)
            candidates = (
                [candidate]
                if candidate
                else [item for _prefix, item in self._seaweedfs_candidates(path)[:2]]
            )
            candidates_by_index[index] = candidates or [path]
            for cand_index, item in enumerate(candidates_by_index[index]):
                url = f"{settings.seaweedfs_base_url}/{item}"
                # 标记行（path序号.候选序号）+ base64；-f 让 404/500 直接失败、不把错误页当图片
                parts.append(
                    f"printf '\\n@@IMG {index}.{cand_index}@@\\n' ; "
                    f"curl -sS -f --max-time 20 {shlex.quote(url)} | base64 | tr -d '\\n' ; "
                    "printf '\\n'"
                )
        try:
            output = self._run(" ; ".join(parts) + " ; true", timeout=max(60, len(pending) * 3))
        except Exception as exc:  # noqa: BLE001
            log_event(
                "db", "WARNING", "image_batch_fetch_failed", count=len(pending), error=str(exc)[:80]
            )
            return out

        blocks: dict[tuple[int, int], str] = {}
        current: tuple[int, int] | None = None
        for line in output.decode("utf-8", errors="replace").splitlines():
            text = line.strip()
            if text.startswith("@@IMG ") and text.endswith("@@"):
                head = text[6:-2].split(".")
                if len(head) != 2 or not all(item.isdigit() for item in head):
                    current = None
                    continue
                current = (int(head[0]), int(head[1]))
                blocks.setdefault(current, "")
                continue
            if current is not None and text:
                blocks[current] = blocks.get(current, "") + text
        for index, path in enumerate(pending):
            payload = ""
            hit_candidate = ""
            for cand_index, candidate in enumerate(candidates_by_index.get(index) or [""]):
                text = blocks.get((index, cand_index)) or ""
                if text:
                    payload, hit_candidate = text, candidate
                    break
            if not payload:
                continue
            try:
                body = base64.b64decode(payload)
            except Exception:  # noqa: BLE001
                continue
            if not body:
                continue
            if hit_candidate:
                self._remember_resolved(path, hit_candidate)
            self._store_image(path, body)
            out[path] = (
                200,
                _IMAGE_CONTENT_TYPES.get(Path(path).suffix.lower(), "image/png"),
                body,
            )
        return out

    def check_seaweedfs(self, path: str) -> tuple[int, str]:
        """只探测对象是否存在（不下载内容），复用同一套候选前缀与缓存。"""
        clean = path.lstrip("/")
        last: tuple[int, str] = (404, "")
        for prefix, candidate in self._seaweedfs_candidates(clean):
            command = (
                "curl -sS -o /dev/null -w '%{http_code} %{content_type}' --max-time 20 "
                f"{shlex.quote(f'{settings.seaweedfs_base_url}/{candidate}')}"
            )
            output = self._run(command, timeout=30).decode("utf-8", errors="replace").strip()
            parts = output.split()
            status = int(parts[0]) if parts and parts[0].isdigit() else 0
            content_type = parts[1] if len(parts) > 1 else ""
            if status == 200:
                self._seaweedfs_prefix = prefix
                return status, content_type
            last = (status, content_type)
        return last

    def check_seaweedfs_many(self, paths: list[str]) -> dict[str, tuple[int, str]]:
        """**一条远程命令**批量探测多个对象，返回 {path: (status, content_type)}。

        为什么要批量：`_run` 是串行加锁的（一条命令一次 SSH 往返），单条探测几十张图就是几十次
        往返——几十秒。实测"看一下病害样图"就因为太慢把检查数量卡在 6 张，用户只能看到 5 张图，
        而表里有 84 条。（每条 path 最多试 2 个候选前缀；同一条 path 有 200 就用 200，
        都不行记最后探测到的状态。）

        **本地缓存也算"取得到"**：`fetch_seaweedfs` 是缓存优先的，缓存里的图 `/api/image`
        照样能渲染出来。以前这里只问远端，于是"缓存里有、远端已删"的图被判成不存在、
        从回答里被剔掉——用户看到的就是"缓存文件夹里明明有 47 张，回答却说没有"。
        """
        cleaned = [str(item or "").lstrip("/") for item in paths]
        cleaned = [path for path in cleaned if path]
        if not cleaned:
            return {}
        best: dict[int, tuple[int, str]] = {}
        # 待探测项要带着**原始下标**：缓存命中的项和待探测项可能交错（实测样图 42 张命中缓存、
        # 夹在 312 个路径中间），用"偏移量"推下标会把某个 200 记到别的路径上。
        pending: list[tuple[int, str]] = []
        for index, path in enumerate(cleaned):
            if self._cached_image_ok(path):
                suffix = Path(path).suffix.lower()
                best[index] = (200, _IMAGE_CONTENT_TYPES.get(suffix, ""))
            else:
                pending.append((index, path))
        if not pending:
            return {path: best.get(index, (0, "")) for index, path in enumerate(cleaned)}
        # 存储先探一次：连不上就别让几十条 curl 各等一遍超时
        try:
            probe = self._run(
                "curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "
                f"{settings.seaweedfs_base_url}/buckets/",
                timeout=20,
            ).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            probe = "0"
        if probe != "200":
            log_event(
                "db", "WARNING", "seaweedfs_unreachable_skip_check", count=len(pending)
            )
            return {path: best.get(index, (0, "")) for index, path in enumerate(cleaned)}

        parts: list[str] = []
        candidates_by_index: dict[int, list[str]] = {}
        for index, path in pending:
            candidates_by_index[index] = [
                candidate for _prefix, candidate in self._seaweedfs_candidates(path)[:2]
            ]
            for cand_index, candidate in enumerate(candidates_by_index[index]):
                url = f"{settings.seaweedfs_base_url}/{candidate}"
                # 把候选序号也打出来：命中 200 的是哪个地址要能对上（批量取图直接用它）
                fmt = f"{index}.{cand_index} %{{http_code}} %{{content_type}}\\n"
                parts.append(
                    "curl -sS -o /dev/null -w "
                    + shlex.quote(fmt)
                    + f" --max-time 8 {shlex.quote(url)}"
                )
        # 末尾 true：个别 curl 失败（连接被拒等）不该让整条命令算失败
        output = self._run(" ; ".join(parts) + " ; true", timeout=max(60, len(pending)))
        for line in output.decode("utf-8", errors="replace").splitlines():
            chunk = line.strip().split()
            if len(chunk) < 2 or "." not in chunk[0]:
                continue
            head = chunk[0].split(".")
            if not head[0].isdigit() or not head[1].isdigit():
                continue
            index, cand_index = int(head[0]), int(head[1])
            status = int(chunk[1]) if chunk[1].isdigit() else 0
            content_type = chunk[2] if len(chunk) > 2 else ""
            current = best.get(index)
            if current is None or current[0] != 200:
                best[index] = (status, content_type)
            if status == 200:
                hit = (candidates_by_index.get(index) or [""])[cand_index : cand_index + 1]
                if hit and hit[0]:
                    self._remember_resolved(cleaned[index], hit[0])
        return {path: best.get(index, (0, "")) for index, path in enumerate(cleaned)}

    def list_seaweedfs(self, path: str = "buckets") -> list[dict[str, Any]]:
        """列出 SeaweedFS 目录（默认从桶根开始），目录在前。"""
        clean = path.strip("/")
        url = f"{settings.seaweedfs_base_url}/{clean}/?pretty=y"
        command = f"curl -sS -m 25 -H 'Accept: application/json' {shlex.quote(url)}"
        output = self._run(command, timeout=40)
        try:
            data = json.loads(output.decode("utf-8", errors="replace") or "{}")
        except Exception as exc:
            raise DatabaseError(f"SeaweedFS 目录列举失败：{exc}") from exc
        entries: list[dict[str, Any]] = []
        for entry in data.get("Entries") or []:
            full = str(entry.get("FullPath") or "").strip("/")
            if not full:
                continue
            if full.split("/")[-1].startswith("."):
                continue  # 隐藏 .system 等内部桶/隐藏项
            mode = int(entry.get("Mode") or 0)
            entries.append(
                {
                    "name": full.split("/")[-1],
                    "path": full,
                    "is_dir": bool(mode & 2147483648),  # Go os.ModeDir 位
                    "size": int(entry.get("FileSize") or 0),
                    "mtime": str(entry.get("Mtime") or ""),
                }
            )
        entries.sort(key=lambda item: (not item["is_dir"], item["name"]))
        return entries


    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None


db_manager = DatabaseManager()
