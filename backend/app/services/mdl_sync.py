"""从数据库自动生成 / 更新 Wren MDL（wren_projects/<项目>/）。

分工：
  * **自动生成**：wren_project.yml + models/<表>/metadata.yml
    表名、列名、类型、可空、主键、表注释、字段注释全部来自 information_schema
    （跟 build_schema_dictionary.py 用的是同一套 schema_catalog 实时读库）。
  * **人工维护**：overrides.yml（模型名、业务描述、计算字段、ref_sql 去重、primary_key）、
    relationships.yml（JOIN 路径）、cubes/（业务指标）——这些是语义，库里没有。

所以"表结构变了"只需要重跑一次：生成器覆盖 models/，把 overrides 合进去，
最后调 `wren context build` 产出 target/mdl.json（子进程里带 PYTHONUTF8=1，
免得中文 MDL 在 GBK 控制台上炸）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from ..config import settings
from . import schema_catalog, wren_engine
from .logging_service import log_error, log_event


# 物理类型 → MDL 类型（Wren 只认它自己的类型名，这里做归族）
_MDL_TYPES: dict[str, str] = {
    "int": "INTEGER", "integer": "INTEGER", "tinyint": "INTEGER", "smallint": "INTEGER",
    "mediumint": "INTEGER", "bigint": "INTEGER", "serial": "INTEGER", "bigserial": "INTEGER",
    "varchar": "VARCHAR", "char": "CHAR", "text": "VARCHAR", "tinytext": "VARCHAR",
    "mediumtext": "VARCHAR", "longtext": "VARCHAR", "character varying": "VARCHAR",
    "character": "VARCHAR", "uuid": "VARCHAR", "enum": "VARCHAR", "set": "VARCHAR",
    "json": "VARCHAR", "jsonb": "VARCHAR", "bytea": "VARCHAR", "blob": "VARCHAR",
    "decimal": "DECIMAL", "numeric": "DECIMAL", "money": "DECIMAL",
    "double": "DOUBLE", "double precision": "DOUBLE", "float": "DOUBLE", "real": "DOUBLE",
    "datetime": "DATETIME", "timestamp": "TIMESTAMP",
    "timestamp without time zone": "TIMESTAMP", "timestamp with time zone": "TIMESTAMP",
    "date": "DATE", "time": "VARCHAR",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
}

PROJECTS: dict[str, str] = {"road_mysql": "mysql", "road_pg": "postgresql"}


def mdl_type(physical_type: str) -> str:
    """物理类型 → MDL 类型；认不出来就退成 VARCHAR（不猜业务含义）。"""
    text = str(physical_type or "").strip().lower().split("(")[0].strip()
    text = text.split(" unsigned")[0].strip()
    return _MDL_TYPES.get(text, "VARCHAR")


def _project_dir(project: str) -> Path:
    return Path(settings.wren_projects_dir) / project


def _load_overrides(project_dir: Path) -> dict[str, Any]:
    path = project_dir / "overrides.yml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _model_metadata(table: dict[str, Any], override: dict[str, Any], kind: str) -> dict[str, Any]:
    """一张表 → 一个模型定义（结构来自库，语义来自 override）。"""
    model: dict[str, Any] = {
        "name": override.get("name") or table["table"],
        "properties": {
            "description": override.get("description")
            or table.get("comment")
            or f"{table['table']}（库侧没写表注释，待补）",
        },
    }
    if override.get("ref_sql"):
        # 去重 / 派生模型：不走物理表，直接给 SQL
        model["ref_sql"] = str(override["ref_sql"]).strip() + "\n"
    else:
        model["table_reference"] = {
            "catalog": "",
            "schema": settings.mysql_database if kind == "mysql" else "public",
            "table": table["table"],
        }
    columns: list[dict[str, Any]] = []
    column_notes: dict[str, Any] = override.get("columns") or {}
    hidden = {str(name) for name in (override.get("hide") or [])}
    for column in table["columns"]:
        # 语义层可以选择不暴露某些物理列（例如会被模型误用的原始外键）
        if column["name"] in hidden:
            continue
        columns.append(
            {
                "name": column["name"],
                "type": mdl_type(column.get("type") or ""),
                "is_calculated": False,
                "properties": {
                    "description": column_notes.get(column["name"])
                    or column.get("comment")
                    or "（库侧没写 COMMENT，待补）"
                },
            }
        )
    for extra in override.get("calculated") or []:
        columns.append(
            {
                "name": extra["name"],
                "type": str(extra.get("type") or "VARCHAR"),
                "is_calculated": True,
                "expression": str(extra["expression"]).strip(),
                "properties": {"description": extra.get("description") or ""},
            }
        )
    # ref_sql 里预先算好的普通列（例如从 JSON 抽的 confidence、关联好的 route_name）
    # 物理表里没有这些列，但模型要暴露给 LLM 直接用。
    for extra in override.get("extra_columns") or []:
        columns.append(
            {
                "name": extra["name"],
                "type": str(extra.get("type") or "VARCHAR"),
                "is_calculated": False,
                "properties": {"description": extra.get("description") or ""},
            }
        )
    # live_catalog 可能已经把上一版 MDL 的派生列带回来，override 里又声明一次；
    # 直接拼接会让 Wren 因 duplicate column 拒绝构建。后声明的 override 定义优先。
    model["columns"] = list(
        {str(column.get("name") or ""): column for column in columns if column.get("name")}.values()
    )
    primary_key = override.get("primary_key") or next(
        (column["name"] for column in table["columns"] if column.get("key") == "PRI"),
        "",
    )
    if primary_key:
        model["primary_key"] = primary_key
        for column in columns:
            if column["name"] == primary_key:
                column["is_primary_key"] = True
    model["cached"] = False
    return model


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=200),
        encoding="utf-8",
    )


def _wren_cli() -> Path:
    """找 wren 可执行文件：优先当前解释器所在环境，其次 PATH，最后项目 .venv。"""
    candidates = [
        # 后端就跑在这个环境里，它的 Scripts/bin 目录一定有 wren（Windows 是 .exe）
        Path(sys.executable).parent / ("wren.exe" if os.name == "nt" else "wren"),
        Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "wren.exe",
        Path(__file__).resolve().parents[2] / ".venv" / "bin" / "wren",
    ]
    found = shutil.which("wren")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _build(project_dir: Path) -> tuple[bool, str]:
    """调 wren context build（子进程 + PYTHONUTF8=1，避免 GBK 读中文 YAML 失败）。"""
    executable = _wren_cli()
    if not executable.exists():
        return False, f"找不到 wren CLI：{executable}（需要 backend\\.venv 这套 Python 3.11 环境）"
    try:
        result = subprocess.run(
            [str(executable), "context", "build"],
            cwd=str(project_dir),
            env={**os.environ, "PYTHONUTF8": "1"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    return result.returncode == 0, output


def build_project(kind: str) -> dict[str, Any]:
    """只重新编译（不改 models/），改完 relationships/cubes 后用它。"""
    project = next((name for name, value in PROJECTS.items() if value == kind), "")
    if not project:
        raise ValueError(f"不支持的数据库类型：{kind}")
    ok, output = _build(_project_dir(project))
    wren_engine.clear_caches()
    return {"project": project, "built": ok, "build_output": output}


def sync_project(kind: str, build: bool = True) -> dict[str, Any]:
    """按 live schema 重新生成一个项目的 models/，合并 overrides，最后 build。"""
    project = next((name for name, value in PROJECTS.items() if value == kind), "")
    if not project:
        raise ValueError(f"不支持的数据库类型：{kind}")
    project_dir = _project_dir(project)
    catalog = schema_catalog.live_catalog(kind)
    model_overrides: dict[str, Any] = (_load_overrides(project_dir).get("models") or {})

    _dump(
        project_dir / "wren_project.yml",
        {
            "schema_version": 5,
            "name": project,
            "catalog": "wren",
            "schema": settings.mysql_database if kind == "mysql" else "public",
            "data_source": "mysql" if kind == "mysql" else "postgres",
        },
    )

    written: list[str] = []
    columns = 0
    for table in catalog["tables"]:
        model = _model_metadata(table, model_overrides.get(str(table["table"])) or {}, kind)
        _dump(project_dir / "models" / str(model["name"]) / "metadata.yml", model)
        written.append(str(model["name"]))
        columns += len(model["columns"])

    # models/ 完全由生成器负责：不再对应任何表的目录直接清掉
    removed: list[str] = []
    models_dir = project_dir / "models"
    if models_dir.exists():
        for folder in sorted(models_dir.iterdir()):
            if folder.is_dir() and folder.name not in written:
                for item in sorted(folder.rglob("*"), reverse=True):
                    item.unlink() if item.is_file() else item.rmdir()
                folder.rmdir()
                removed.append(folder.name)

    result: dict[str, Any] = {
        "project": project,
        "kind": kind,
        "models": len(written),
        "columns": columns,
        "removed": removed,
        "built": False,
        "build_output": "",
    }
    if build:
        ok, output = _build(project_dir)
        result["built"] = ok
        result["build_output"] = output
        if not ok:
            log_error("mdl", "build_failed", RuntimeError(output), project=project)
    # 进程内的缓存要清掉，否则"重建语义层"按钮点了也不生效（得重启）：
    # MDL / Wren 引擎、字段速查（库结构变了，字段字典也得重读）
    wren_engine.clear_caches()
    from . import clarify  # 局部导入避免模块级循环依赖

    clarify._catalog.cache_clear()  # 字段速查也是进程内缓存，库结构变了要重读
    log_event(
        "mdl", "INFO", "sync_done",
        project=project, models=result["models"], built=result["built"],
    )
    return result


def sync_all(build: bool = True) -> list[dict[str, Any]]:
    return [sync_project(kind, build=build) for kind in PROJECTS.values()]
