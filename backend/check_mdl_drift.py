"""MDL 漂移体检：把 wren_projects 里的模型跟真实库结构对一遍。

为什么需要它：wren_projects/ 下的 MDL 是**人工建模**的语义层（业务模型、关系、指标），
不是 schema 的镜像。数据库加列/改列名/改类型/删表之后，MDL 不会自己跟着变，
要等线上查询报错才发现就太晚了。这个脚本把差异提前列出来。

用法（不需要 PYTHONUTF8，脚本自己按 utf-8 读 YAML）：
    cd backend
    .\\.venv\\Scripts\\python.exe check_mdl_drift.py                  # 体检两个项目
    .\\.venv\\Scripts\\python.exe check_mdl_drift.py --project road_mysql
    .\\.venv\\Scripts\\python.exe check_mdl_drift.py --draft ai_work_order   # 给表生成建模草稿

退出码：0 = 没有破坏性问题；1 = 有（表/列不存在、模型编译不过）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")

import yaml

from app.config import PROJECT_ROOT, settings
from app.services import schema_catalog, wren_engine


# 物理类型 → MDL 类型族，只比族（int(11) 和 INTEGER 都算 INTEGER）
_TYPE_FAMILY: dict[str, str] = {
    "int": "INTEGER", "integer": "INTEGER", "tinyint": "INTEGER", "smallint": "INTEGER",
    "mediumint": "INTEGER", "bigint": "INTEGER", "serial": "INTEGER", "bigserial": "INTEGER",
    "varchar": "VARCHAR", "char": "CHAR", "text": "VARCHAR", "tinytext": "VARCHAR",
    "mediumtext": "VARCHAR", "longtext": "VARCHAR", "character varying": "VARCHAR",
    "character": "VARCHAR", "uuid": "VARCHAR", "json": "VARCHAR", "jsonb": "VARCHAR",
    "enum": "VARCHAR", "set": "VARCHAR", "nvarchar": "VARCHAR",
    "decimal": "DECIMAL", "numeric": "DECIMAL", "money": "DECIMAL",
    "double": "DOUBLE", "double precision": "DOUBLE", "float": "DOUBLE", "real": "DOUBLE",
    "datetime": "DATETIME", "timestamp": "TIMESTAMP", "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMP",
    "date": "DATE",
    "time": "VARCHAR",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
    "blob": "VARCHAR", "bytea": "VARCHAR",
}


def _family(physical_type: str) -> str:
    text = str(physical_type or "").strip().lower()
    text = text.split("(")[0].strip()          # int(11) → int
    text = text.split(" unsigned")[0].strip()
    return _TYPE_FAMILY.get(text, text.upper())


def _load_models(project: Path) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for folder in ("models", "views"):
        for path in sorted((project / folder).glob("*/metadata.yml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # noqa: BLE001
                models.append({"name": f"<读取失败 {path.name}>", "_error": str(exc), "_path": path})
                continue
            data["_path"] = path
            models.append(data)
    return models


def _physical_columns(catalog: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        str(table["table"]): {
            str(column["name"]): str(column.get("type") or "") for column in table["columns"]
        }
        for table in catalog["tables"]
    }


def check_project(project: Path, kind: str, verbose: bool = False) -> dict[str, list[str]]:
    """返回 {"breaking": [...], "warn": [...], "info": [...]}。"""
    findings: dict[str, list[str]] = {"breaking": [], "warn": [], "info": []}
    catalog = schema_catalog.live_catalog(kind)
    tables = _physical_columns(catalog)
    # 编译验证只能验证"配置里的那个项目"（wren_engine 认的是 settings.wren_projects_dir），
    # 传别的目录（比如改坏的副本）时跳过，只做结构比对。
    configured = (Path(settings.wren_projects_dir) / project.name).resolve() == project.resolve()
    can_compile = configured and wren_engine.available(kind)
    if not configured:
        findings["info"].append("非标准项目路径：跳过 Wren 编译验证，只比对结构")
    for model in _load_models(project):
        name = model.get("name")
        if model.get("_error"):
            findings["breaking"].append(f"{name}: YAML 读取失败 —— {model['_error']}")
            continue
        # 真正的验收：让 Wren 编译一句最小查询，能编译说明这个模型的列/SQL 都还成立
        if can_compile and str(name).strip():
            probe = wren_engine.compile_mdl_sql(kind, f"SELECT COUNT(*) AS count FROM {name}")
            if not probe:
                findings["breaking"].append(
                    f"{name}: Wren 编译不过（列被删/改名、expression 或 ref_sql 失效），"
                    "看 logs/app/app.log 里的 compile_failed"
                )
        reference = model.get("table_reference") or {}
        table = str(reference.get("table") or "")
        declared = {
            str(column["name"]): column
            for column in model.get("columns") or []
            if column.get("name")
        }
        if not table:
            # ref_sql / statement 模型：直接让编译器跑一次，能编译就说明 SQL 还成立
            findings["info"].append(
                f"{name}: 非表模型（{ 'ref_sql' if model.get('ref_sql') else 'view' }），"
                "改动后必须重新 `wren context build`"
            )
            continue
        if table not in tables:
            findings["breaking"].append(f"{name}: 物理表 {table} 在库里不存在（改名或已删？）")
            continue
        live = tables[table]
        for column, spec in declared.items():
            if spec.get("is_calculated"):
                continue
            physical_name = str(spec.get("expression") or column) if not spec.get("is_calculated") else column
            if physical_name not in live:
                findings["breaking"].append(
                    f"{name}.{column}: 物理列 {physical_name} 没有了（改名/删除？模型会编译失败）"
                )
                continue
            mdl_family = str(spec.get("type") or "").upper()
            live_family = _family(live[physical_name])
            if mdl_family and mdl_family != live_family:
                findings["warn"].append(
                    f"{name}.{column}: MDL 写的是 {mdl_family}，库里是 {live[physical_name]}（{live_family}）"
                )
        unmodelled = sorted(set(live) - {str(spec.get("expression") or c) for c, spec in declared.items()})
        if unmodelled:
            detail = "：" + "、".join(unmodelled[:40]) if verbose else "（要清单加 --verbose）"
            findings["info"].append(
                f"{name}: 表 {table} 有 {len(unmodelled)} 个列没进模型，可以不管，需要时再加{detail}"
            )
    return findings


def draft_model(kind: str, table: str) -> str:
    """按真实结构生成一份 metadata.yml 草稿（列名/类型/中文注释全部来自 information_schema）。"""
    catalog = schema_catalog.live_catalog(kind)
    for item in catalog["tables"]:
        if str(item["table"]) != table:
            continue
        lines = [
            f"name: {table}",
            "properties:",
            f"  description: {item.get('comment') or '（待补充业务说明）'}",
            "table_reference:",
            "  catalog: \"\"",
            f"  schema: {settings.mysql_database if kind == 'mysql' else 'public'}",
            f"  table: {table}",
            "columns:",
        ]
        for column in item["columns"]:
            column_type = str(column.get("type") or "")
            lines.append(f"  - name: {column['name']}")
            lines.append(f"    type: {_family(column_type) if _family(column_type) in {'INTEGER','VARCHAR','DECIMAL','DOUBLE','TIMESTAMP','DATE','BOOLEAN'} else 'VARCHAR'}")
            lines.append("    is_calculated: false")
            lines.append(
                f"    properties: {{description: {column.get('comment') or '（待补充）'}}}"
            )
        names = {str(column["name"]) for column in item["columns"]}
        if "id" in names:
            lines.append("primary_key: id")
        else:
            lines.append("# 这张表没有 id 列：确认主键后自己填 primary_key（时序表也可以不要）")
        lines.append("cached: false")
        return "\n".join(lines)
    return f"# 库里没有表 {table}（或者它不在白名单 config.mysql_allowed_tables / pg_allowed_tables 里）"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 wren_projects 的 MDL 跟真实库结构的差异")
    parser.add_argument("--project", default="", help="road_mysql / road_pg / 目录路径，默认两个都查")
    parser.add_argument("--draft", default="", help="给某张表生成 metadata.yml 草稿")
    parser.add_argument("--verbose", action="store_true", help="列出未建模的列清单")
    args = parser.parse_args()

    kinds = {"road_mysql": "mysql", "road_pg": "postgresql"}
    projects = [args.project] if args.project else list(kinds)

    if args.draft:
        kind = kinds.get(projects[0], "mysql")
        if not kind:
            custom = Path(projects[0]) / "wren_project.yml"
            kind = "postgresql" if "postgres" in custom.read_text(encoding="utf-8") else "mysql"
        print(draft_model(kind, args.draft))
        return 0

    failed = False
    for name in projects:
        kind = kinds.get(name)
        project = Path(settings.wren_projects_dir) / name
        if not kind:
            # 允许传目录：按项目里的 data_source 判断库类型
            project = Path(name)
            if not project.exists():
                print(f"[跳过] {name}：不存在")
                continue
            config = project / "wren_project.yml"
            text = config.read_text(encoding="utf-8") if config.exists() else ""
            kind = "postgresql" if "data_source: postgres" in text else "mysql"
            display = str(project)
        else:
            display = name
        if not project.exists():
            print(f"[跳过] {name}：不存在（{project}）")
            continue
        print(f"=== {display}（{kind}）===")
        findings = check_project(project, kind, verbose=args.verbose)
        if findings["breaking"]:
            failed = True
            print("  [X] 会导致查询失败：")
            for line in findings["breaking"]:
                print(f"    - {line}")
        if findings["warn"]:
            print("  [!] 类型对不上（多数能跑，但要确认）：")
            for line in findings["warn"]:
                print(f"    - {line}")
        if findings["info"]:
            print("  [i] 提示：")
            for line in findings["info"]:
                print(f"    - {line}")
        if not any(findings.values()):
            print("  [OK] 与库结构一致")
        if not findings["breaking"]:
            print("  产物是否最新：改过 MDL 记得 `wren context build` 再重启后端")
    print(f"\nPROJECT_ROOT={PROJECT_ROOT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
