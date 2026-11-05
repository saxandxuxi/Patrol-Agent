"""官方流程版 MDL 生成器（与原 `mdl_sync.py` 并存，用 `MDL_GENERATOR` 切换）。

按官方 `wren-generate-mdl` 的七阶段编排：
  Connect → Discover → Normalize → Scaffold → Validate → Build →（Index 不做）→ 迭代(overrides)
与原生成器的**实质差异**（就是为了 A/B 对比这些）：
  1. **类型归一走官方 CLI**：`wren utils parse-types --dialect …`（一次批量），失败才退回原文件的
     手写字典 `mdl_sync.mdl_type`；两者结果不一样的地方记进 `type_diffs`（实测 MySQL 的
     `int(11) unsigned` 官方给 `UINT(11)`、原字典给 `INTEGER`）。
  2. **先 validate 再 build**：`wren context validate` 不过就不 build，接口如实返回 `valid=False`。
  3. **外键关系草稿**：先从库里真外键生成；本库实测 MySQL/PG 都是 **0 条真外键**（项目靠业务字段
     关联），退回项目自己的关系知识（`schema_catalog` 的外键映射）生成草稿，人工
     `relationships.yml` 优先（同名人工覆盖自动）。
  4. **Scaffold 走官方 `wren context init --empty`**（项目目录还不存在时），再写 `wren_project.yml`。

两处与原方案的偏差（如实记下）：
  · 发现阶段用 information_schema（`schema_catalog.live_catalog`）而不是 SQLAlchemy `inspect()`：
    项目连库是走 **SSH 隧道**的（本地没有 MySQL/PG 端口，实测直连超时），SQLAlchemy 连不上；
    而且两个库都**没有建外键约束**，inspect 能拿到的信息与 information_schema 完全一致。
  · 表集合沿用 `live_catalog` 的白名单过滤 —— 与旧生成器保持同一张表清单，对比才公平。

原文件 `mdl_sync.py` 不改；override 合并（名称/描述/计算字段/extra_columns/hide/ref_sql/主键）
直接复用它的 `_model_metadata`，保证两边语义一致、不会各写一套。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from ..config import settings
from . import mdl_sync, schema_catalog, wren_engine
from .db import DatabaseError, db_manager
from .logging_service import log_error, log_event

PROJECTS: dict[str, str] = mdl_sync.PROJECTS


def _project_dir(project: str) -> Path:
    return mdl_sync._project_dir(project)


def _wren_cli() -> Path:
    return mdl_sync._wren_cli()


def _run_wren(
    args: list[str], cwd: Path | None = None, stdin: str = "", timeout: int = 180
) -> tuple[bool, str]:
    """调官方 wren CLI（子进程 + PYTHONUTF8=1，防止中文 YAML 在 GBK 控制台炸）。"""
    executable = _wren_cli()
    if not executable.exists():
        return False, f"找不到 wren CLI：{executable}"
    try:
        result = subprocess.run(
            [str(executable), *args],
            cwd=str(cwd) if cwd else None,
            input=stdin or None,
            env={**os.environ, "PYTHONUTF8": "1"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    return result.returncode == 0, output


def _discover(kind: str) -> dict[str, Any]:
    """阶段 2 Discover：白名单表的表/列/类型/主键（同旧生成器，见模块头的偏差说明）。"""
    return schema_catalog.live_catalog(kind)


def _real_foreign_keys(kind: str) -> list[dict[str, str]]:
    """阶段 2 的外键部分：库里真实存在的外键约束（没有就返回空）。"""
    try:
        if kind == "mysql":
            database = settings.mysql_database.replace("'", "''")
            rows = db_manager.query(
                "mysql",
                "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                f"WHERE TABLE_SCHEMA = '{database}' AND REFERENCED_TABLE_NAME IS NOT NULL",
            )["rows"]
        else:
            rows = db_manager.query(
                "postgresql",
                "SELECT tc.table_name AS TABLE_NAME, kcu.column_name AS COLUMN_NAME, "
                "ccu.table_name AS REFERENCED_TABLE_NAME, "
                "ccu.column_name AS REFERENCED_COLUMN_NAME "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = tc.constraint_name "
                " AND kcu.table_schema = tc.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                " AND ccu.table_schema = tc.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'",
            )["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("mdl_official", "foreign_key_read_failed", exc, kind=kind)
        return []
    return [
        {
            "table": str(row.get("TABLE_NAME") or ""),
            "column": str(row.get("COLUMN_NAME") or ""),
            "ref_table": str(row.get("REFERENCED_TABLE_NAME") or ""),
            "ref_column": str(row.get("REFERENCED_COLUMN_NAME") or ""),
        }
        for row in rows
        if row.get("TABLE_NAME") and row.get("COLUMN_NAME") and row.get("REFERENCED_TABLE_NAME")
    ]


def _parse_types(raw_types: list[str], dialect: str) -> dict[str, str]:
    """阶段 3 Normalize：官方 `wren utils parse-types` 批量归一，失败退手写字典。"""
    wanted = sorted({str(item or "").strip() for item in raw_types if str(item or "").strip()})
    if not wanted:
        return {}
    payload = json.dumps(
        [{"column": f"c{index}", "raw_type": raw} for index, raw in enumerate(wanted)],
        ensure_ascii=False,
    )
    ok, output = _run_wren(
        ["utils", "parse-types", "--dialect", dialect], stdin=payload, timeout=120
    )
    official: dict[str, str] = {}
    if ok:
        try:
            for item in json.loads(output or "[]"):
                raw = str(item.get("raw_type") or "").strip()
                parsed = str(item.get("type") or "").strip()
                if raw and parsed:
                    official[raw] = parsed
        except Exception as exc:  # noqa: BLE001
            log_error("mdl_official", "parse_types_unparsable", exc, output=output[:200])
    result: dict[str, str] = {}
    for raw in wanted:
        fallback = mdl_sync.mdl_type(raw)
        result[raw] = official.get(raw) or fallback
    return result


def _scaffold(project_dir: Path, kind: str) -> str:
    """阶段 4 Scaffold：项目还没有骨架时用官方 `wren context init --empty` 起一个。"""
    note = ""
    if not (project_dir / "wren_project.yml").exists():
        project_dir.mkdir(parents=True, exist_ok=True)
        ok, output = _run_wren(["context", "init", "--path", str(project_dir), "--empty"])
        note = "context_init_ok" if ok else f"context_init_failed: {output[:120]}"
        if not ok:
            log_error("mdl_official", "context_init_failed", RuntimeError(output[:200]))
    mdl_sync._dump(
        project_dir / "wren_project.yml",
        {
            "schema_version": 5,
            "name": project_dir.name,
            "catalog": "wren",
            "schema": settings.mysql_database if kind == "mysql" else "public",
            "data_source": "mysql" if kind == "mysql" else "postgres",
        },
    )
    return note


def _model_names(catalog: dict[str, Any], overrides: dict[str, Any]) -> dict[str, str]:
    """物理表 → 模型名（override 里改了名字的按改后的算）。"""
    names: dict[str, str] = {}
    for table in catalog.get("tables") or []:
        physical = str(table.get("table") or "")
        model = (overrides.get(physical) or {}).get("name") or physical
        names[physical] = str(model)
    return names


def _relationships_draft(catalog: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """关系草稿：优先库里真外键；没有就退回项目自己的关系知识（`schema_catalog` 的外键映射）。"""
    names = _model_names(catalog, overrides)
    models = set(names.values())
    draft: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _push(table: str, column: str, ref_table: str, ref_column: str) -> None:
        source, target = names.get(table), names.get(ref_table)
        if not source or not target or source not in models or target not in models:
            return
        name = f"{source}_{target}"
        if name in seen:
            return
        seen.add(name)
        draft.append(
            {
                "name": name,
                "models": [source, target],
                "join_type": "MANY_TO_ONE",
                "condition": f"{source}.{column} = {target}.{ref_column}",
            }
        )

    fks = _real_foreign_keys(str(catalog.get("kind") or "mysql"))
    for item in fks:
        _push(item["table"], item["column"], item["ref_table"], item["ref_column"])
    if draft:
        return draft

    # 库里没建外键（本项目实测就是 0 条）→ 用项目自己的关系知识生成草稿，人工文件仍然优先
    for table in catalog.get("tables") or []:
        physical = str(table.get("table") or "")
        columns = {str(col.get("name") or "") for col in table.get("columns") or []}
        for column in sorted(columns):
            target = schema_catalog._FOREIGN_KEYS.get(column)  # noqa: SLF001 —— 项目自己的关系表
            if not target:
                continue
            # 元组是 (目标表, 显示列, 连接列)：关系条件要用**连接列**（一般是 id），
            # 用显示列（short_name/full_name）会生成"route_id = route.short_name"这种错 JOIN。
            join_column = str(target[2] or "id")
            _push(physical, column, str(target[0]), join_column)
    return draft


def _merge_relationships(project_dir: Path, draft: list[dict[str, Any]]) -> dict[str, int]:
    """人工 `relationships.yml` 优先，自动草稿只补它没有的；写回时标出来源。"""
    path = project_dir / "relationships.yml"
    manual: list[dict[str, Any]] = []
    header = "# Wren MDL 关系定义：编译器据此自动加 JOIN，LLM 不需要写 JOIN\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        # 头部注释只取文件开头连续的那几行（别把下面的 `# source:` 标记也当头部，
        # 否则每跑一次就把它往头部堆一遍）
        head_lines: list[str] = []
        for line in lines:
            if line.strip().startswith("#"):
                head_lines.append(line)
            elif line.strip():
                break
        if head_lines:
            header = "\n".join(head_lines) + "\n"
        try:
            loaded = list((yaml.safe_load(text) or {}).get("relationships") or [])
        except Exception as exc:  # noqa: BLE001
            # 人工文件解析不了就**一个字都不写**：宁可这次不合并，也不能把人工维护的关系覆盖掉
            # （上一版在这里退化成"当 0 条处理"，结果把 6 条人工关系冲掉了）。
            log_error("mdl_official", "relationships_unparsable", exc, file=str(path))
            raise
        # `# source: auto` 标记过的条目由生成器负责：重跑时按最新草稿重写，
        # 不当成人工维护（否则草稿改了刷不进去——上一版就踩了这个）。
        auto_names: set[str] = set()
        pending_auto = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# source: auto"):
                pending_auto = True
                continue
            if stripped.startswith("- name: "):
                if pending_auto:
                    auto_names.add(stripped[len("- name: ") :].strip())
                pending_auto = False
            elif stripped and not stripped.startswith("#"):
                pending_auto = False
        manual = [item for item in loaded if str(item.get("name") or "") not in auto_names]
    taken = {str(item.get("name") or "") for item in manual}
    added = [item for item in draft if str(item.get("name") or "") not in taken]
    if not added:
        return {"manual": len(manual), "auto": 0, "added": 0}
    # 整份结构交给 yaml.safe_dump（自己拼字符串缩进很容易写出坏 YAML——上一版就踩了），
    # 再在每个条目上面插一行来源注释（注释会被解析器忽略，不污染 schema）。
    source_of = {str(item.get("name") or ""): "manual" for item in manual}
    source_of.update({str(item.get("name") or ""): "auto" for item in added})
    dumped = yaml.safe_dump(
        {"relationships": manual + added}, allow_unicode=True, sort_keys=False, width=200
    )
    lines: list[str] = []
    for line in dumped.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name: "):
            name = stripped[len("- name: ") :].strip()
            indent = line[: len(line) - len(line.lstrip())]
            source = source_of.get(name, "manual")
            note = "人工维护" if source == "manual" else "生成器草稿（人工可覆盖）"
            lines.append(f"{indent}  # source: {source} {note}")
        lines.append(line)
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return {"manual": len(manual), "auto": len(added), "added": len(added)}


def _clean_removed_models(models_dir: Path, written: list[str]) -> list[str]:
    """models/ 由生成器负责：不再对应任何表的目录清掉。"""
    removed: list[str] = []
    if not models_dir.exists():
        return removed
    for folder in sorted(models_dir.iterdir()):
        if folder.is_dir() and folder.name not in written:
            for item in sorted(folder.rglob("*"), reverse=True):
                item.unlink() if item.is_file() else item.rmdir()
            folder.rmdir()
            removed.append(folder.name)
    return removed


def _clear_caches() -> None:
    """build 之后清进程内缓存，否则不重启不生效。"""
    wren_engine.clear_caches()
    from . import clarify  # 局部导入避免模块级循环依赖

    clarify._catalog.cache_clear()  # noqa: SLF001


def _validate(project_dir: Path) -> tuple[bool, str]:
    return _run_wren(["context", "validate", "--path", str(project_dir)], timeout=180)


def _build(project_dir: Path) -> tuple[bool, str]:
    return _run_wren(["context", "build"], cwd=project_dir, timeout=300)


def sync_project(kind: str, build: bool = True) -> dict[str, Any]:
    """按官方七阶段重新生成一个项目的 models/，合并 overrides，validate 后 build。"""
    started = time.perf_counter()
    project = next((name for name, value in PROJECTS.items() if value == kind), "")
    if not project:
        raise ValueError(f"不支持的数据库类型：{kind}")
    project_dir = _project_dir(project)
    catalog = _discover(kind)
    model_overrides: dict[str, Any] = (mdl_sync._load_overrides(project_dir).get("models") or {})

    scaffold_note = _scaffold(project_dir, kind)

    # 阶段 3 Normalize：先把全库类型批量归一，后面写模型直接查表
    raw_types = [
        str(column.get("type") or "")
        for table in catalog.get("tables") or []
        for column in table.get("columns") or []
    ]
    type_map = _parse_types(raw_types, "mysql" if kind == "mysql" else "postgres")
    type_diffs: list[str] = []

    written: list[str] = []
    columns = 0
    for table in catalog.get("tables") or []:
        override = model_overrides.get(str(table.get("table"))) or {}
        model = mdl_sync._model_metadata(table, override, kind)
        physical = {
            str(column.get("name") or ""): str(column.get("type") or "")
            for column in table.get("columns") or []
        }
        for column in model["columns"]:
            name = str(column.get("name") or "")
            if name not in physical:  # 计算字段 / ref_sql 里的额外列：保持 override 里写的类型
                continue
            official = type_map.get(physical[name]) or mdl_sync.mdl_type(physical[name])
            legacy = mdl_sync.mdl_type(physical[name])
            if official != legacy:
                type_diffs.append(f"{table.get('table')}.{name}: 官方 {official} / 旧 {legacy}（{physical[name]}）")
            column["type"] = official
        mdl_sync._dump(project_dir / "models" / str(model["name"]) / "metadata.yml", model)
        written.append(str(model["name"]))
        columns += len(model["columns"])

    removed = _clean_removed_models(project_dir / "models", written)
    relations = _merge_relationships(project_dir, _relationships_draft(catalog, model_overrides))

    valid, validate_output = _validate(project_dir)
    result: dict[str, Any] = {
        "project": project,
        "kind": kind,
        "generator": "official",
        "models": len(written),
        "columns": columns,
        "removed": removed,
        "type_diffs": type_diffs,
        "relationships": relations,
        "valid": valid,
        "validate_output": validate_output,
        "built": False,
        "build_output": "",
        "scaffold": scaffold_note,
        "duration_ms": 0,
    }
    if valid and build:
        ok, output = _build(project_dir)
        result["built"] = ok
        result["build_output"] = output
        if not ok:
            log_error("mdl_official", "build_failed", RuntimeError(output[:400]), project=project)
    elif not valid:
        log_error(
            "mdl_official", "validate_failed", RuntimeError(validate_output[:400]), project=project
        )
    _clear_caches()
    result["duration_ms"] = round((time.perf_counter() - started) * 1000)
    log_event(
        "mdl_official",
        "INFO",
        "sync_done",
        project=project,
        models=result["models"],
        valid=valid,
        built=result["built"],
        type_diffs=len(type_diffs),
    )
    return result


def sync_all(build: bool = True) -> list[dict[str, Any]]:
    return [sync_project(kind, build=build) for kind in PROJECTS.values()]


def build_project(kind: str) -> dict[str, Any]:
    """只重新校验 + 编译（不改 models/）。"""
    project = next((name for name, value in PROJECTS.items() if value == kind), "")
    if not project:
        raise ValueError(f"不支持的数据库类型：{kind}")
    project_dir = _project_dir(project)
    valid, validate_output = _validate(project_dir)
    ok, output = _build(project_dir) if valid else (False, "")
    _clear_caches()
    return {
        "project": project,
        "generator": "official",
        "valid": valid,
        "validate_output": validate_output,
        "built": ok,
        "build_output": output,
    }
