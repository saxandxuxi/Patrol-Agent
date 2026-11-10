from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, settings
from app.services.db import db_manager
from app.services.knowledge_sync import sync_dataset


CORE_MYSQL_TABLES = (
    "ai_problem",
    "ai_problem_base",
    "ai_asset_main",
    "ai_asset_base",
    "ai_distance_capture",
    "basic_facilities_routes",
    "basic_facilities_sections",
    "basic_facilities_sections_pile_detail",
    "basic_facilities_bridges",
    "basic_facilities_tunnels",
    "basic_facilities_slope",
    "project_manage_project_main",
    "maintenance_project",
    "maintenance_road_work_record",
    "ai_work_order",
    "maintenance_work_order",
    "ai_maintain_patrol",
    "ai_maintain_routes_patrol",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def _rows(kind: str, sql: str) -> list[dict[str, Any]]:
    return db_manager.query(kind, sql)["rows"]


def _mysql_dictionary() -> str:
    database = settings.mysql_database.replace("'", "''")
    allowed = set(CORE_MYSQL_TABLES)
    tables = _rows(
        "mysql",
        "SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS, ENGINE, TABLE_COLLATION "
        "FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = '{database}' ORDER BY TABLE_NAME",
    )
    tables = [table for table in tables if str(table.get("TABLE_NAME") or "") in allowed]
    columns = _rows(
        "mysql",
        "SELECT TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
        "COLUMN_DEFAULT, COLUMN_KEY, EXTRA, COLUMN_COMMENT "
        "FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{database}' ORDER BY TABLE_NAME, ORDINAL_POSITION",
    )
    columns = [
        column
        for column in columns
        if str(column.get("TABLE_NAME") or "") in allowed
    ]
    indexes = _rows(
        "mysql",
        "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE "
        "FROM information_schema.STATISTICS "
        f"WHERE TABLE_SCHEMA = '{database}' ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
    )
    indexes = [
        index
        for index in indexes
        if str(index.get("TABLE_NAME") or "") in allowed
    ]
    foreign_keys = _rows(
        "mysql",
        "SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME, "
        "REFERENCED_COLUMN_NAME "
        "FROM information_schema.KEY_COLUMN_USAGE "
        f"WHERE TABLE_SCHEMA = '{database}' AND REFERENCED_TABLE_NAME IS NOT NULL "
        "ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION",
    )
    foreign_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if str(foreign_key.get("TABLE_NAME") or "") in allowed
    ]

    exact_counts: dict[str, str] = {}
    for table in CORE_MYSQL_TABLES:
        try:
            exact_counts[table] = str(
                _rows("mysql", f"SELECT COUNT(*) AS c FROM `{table}`")[0]["c"]
            )
        except Exception:
            continue

    columns_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for column in columns:
        columns_by_table[column["TABLE_NAME"]].append(column)
    indexes_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index in indexes:
        indexes_by_table[index["TABLE_NAME"]].append(index)
    fks_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for foreign_key in foreign_keys:
        fks_by_table[foreign_key["TABLE_NAME"]].append(foreign_key)

    lines = [
        f"# MySQL 数据字典：{settings.mysql_database}",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"表数量：{len(tables)}",
        "",
        "说明：`TABLE_ROWS` 来自 information_schema，InnoDB 表为估算值；核心表额外给出精确行数。",
        "",
    ]

    if exact_counts:
        lines.extend(["## 核心表精确行数", "", "| 表名 | 精确行数 |", "|---|---:|"])
        for table, count in exact_counts.items():
            lines.append(f"| `{table}` | {count} |")
        lines.append("")

    lines.extend(
        [
            "## 全部表清单",
            "",
            "| 表名 | 说明 | 估算行数 | 引擎 | 排序规则 |",
            "|---|---|---:|---|---|",
        ]
    )
    for table in tables:
        lines.append(
            f"| `{_text(table['TABLE_NAME'])}` | {_text(table['TABLE_COMMENT'])} | "
            f"{_text(table['TABLE_ROWS'])} | {_text(table['ENGINE'])} | "
            f"{_text(table['TABLE_COLLATION'])} |"
        )
    lines.append("")

    for table in tables:
        name = table["TABLE_NAME"]
        lines.extend([f"## `{name}`", ""])
        comment = _text(table["TABLE_COMMENT"])
        if comment:
            lines.extend([f"表说明：{comment}", ""])
        lines.extend(
            [
                f"- 估算行数：{_text(table['TABLE_ROWS'])}",
                f"- 引擎：{_text(table['ENGINE'])}",
                f"- 排序规则：{_text(table['TABLE_COLLATION'])}",
                "",
                "| 序号 | 字段 | 类型 | 可空 | 默认值 | 键 | 额外 | 说明 |",
                "|---:|---|---|---|---|---|---|---|",
            ]
        )
        for column in columns_by_table.get(name, []):
            lines.append(
                f"| {_text(column['ORDINAL_POSITION'])} | `{_text(column['COLUMN_NAME'])}` | "
                f"{_text(column['COLUMN_TYPE'])} | {_text(column['IS_NULLABLE'])} | "
                f"{_text(column['COLUMN_DEFAULT'])} | {_text(column['COLUMN_KEY'])} | "
                f"{_text(column['EXTRA'])} | {_text(column['COLUMN_COMMENT'])} |"
            )
        lines.append("")

        table_indexes = indexes_by_table.get(name, [])
        if table_indexes:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for index in table_indexes:
                grouped[index["INDEX_NAME"]].append(index)
            lines.extend(["**索引**", "", "| 索引名 | 唯一 | 类型 | 字段顺序 |", "|---|---|---|---|"])
            for index_name, parts in grouped.items():
                parts = sorted(parts, key=lambda item: int(item["SEQ_IN_INDEX"]))
                fields = ", ".join(f"`{_text(part['COLUMN_NAME'])}`" for part in parts)
                unique = "是" if str(parts[0]["NON_UNIQUE"]) == "0" else "否"
                lines.append(
                    f"| `{_text(index_name)}` | {unique} | {_text(parts[0]['INDEX_TYPE'])} | {fields} |"
                )
            lines.append("")

        table_fks = fks_by_table.get(name, [])
        if table_fks:
            lines.extend(["**外键**", "", "| 约束名 | 字段 | 引用表 | 引用字段 |", "|---|---|---|---|"])
            for foreign_key in table_fks:
                lines.append(
                    f"| `{_text(foreign_key['CONSTRAINT_NAME'])}` | "
                    f"`{_text(foreign_key['COLUMN_NAME'])}` | "
                    f"`{_text(foreign_key['REFERENCED_TABLE_NAME'])}` | "
                    f"`{_text(foreign_key['REFERENCED_COLUMN_NAME'])}` |"
                )
            lines.append("")
    return "\n".join(lines)


def _pg_dictionary() -> str:
    tables = _rows(
        "postgresql",
        "SELECT table_name, "
        "obj_description(('public.' || table_name)::regclass, 'pg_class') AS comment "
        "FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name",
    )
    columns = _rows(
        "postgresql",
        "SELECT table_name, ordinal_position, column_name, data_type, udt_name, "
        "is_nullable, column_default, "
        "col_description(('public.' || table_name)::regclass, ordinal_position) AS comment "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position",
    )
    indexes = _rows(
        "postgresql",
        "SELECT tablename, indexname, indexdef "
        "FROM pg_indexes WHERE schemaname = 'public' ORDER BY tablename, indexname",
    )
    constraints = _rows(
        "postgresql",
        "SELECT tc.table_name, tc.constraint_name, tc.constraint_type, kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.table_schema = kcu.table_schema "
        "WHERE tc.table_schema = 'public' "
        "  AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY') "
        "ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position",
    )

    columns_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for column in columns:
        columns_by_table[column["table_name"]].append(column)
    indexes_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index in indexes:
        indexes_by_table[index["tablename"]].append(index)
    constraints_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for constraint in constraints:
        constraints_by_table[constraint["table_name"]].append(constraint)

    lines = [
        f"# PostgreSQL 数据字典：{settings.pg_database}",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"表数量：{len(tables)}",
        "",
    ]
    for table in tables:
        name = table["table_name"]
        count = ""
        try:
            count = str(_rows("postgresql", f'SELECT COUNT(*) AS c FROM "{name}"')[0]["c"])
        except Exception:
            pass
        lines.extend([f"## `{name}`", ""])
        if table.get("comment"):
            lines.extend([f"表说明：{_text(table['comment'])}", ""])
        lines.extend(
            [
                f"- 精确行数：{count}",
                "",
                "| 序号 | 字段 | 类型 | 可空 | 默认值 | 说明 |",
                "|---:|---|---|---|---|---|",
            ]
        )
        for column in columns_by_table.get(name, []):
            lines.append(
                f"| {_text(column['ordinal_position'])} | `{_text(column['column_name'])}` | "
                f"{_text(column['data_type'])} | {_text(column['is_nullable'])} | "
                f"{_text(column['column_default'])} | {_text(column['comment'])} |"
            )
        lines.append("")

        if constraints_by_table.get(name):
            lines.extend(["**约束**", "", "| 约束名 | 类型 | 字段 |", "|---|---|---|"])
            for constraint in constraints_by_table[name]:
                lines.append(
                    f"| `{_text(constraint['constraint_name'])}` | "
                    f"{_text(constraint['constraint_type'])} | "
                    f"`{_text(constraint['column_name'])}` |"
                )
            lines.append("")

        if indexes_by_table.get(name):
            lines.extend(["**索引**", "", "| 索引名 | 定义 |", "|---|---|"])
            for index in indexes_by_table[name]:
                lines.append(f"| `{_text(index['indexname'])}` | {_text(index['indexdef'])} |")
            lines.append("")
    return "\n".join(lines)


def _seaweedfs_dictionary() -> str:
    return "\n".join(
        [
            "# SeaweedFS 对象存储",
            "",
            f"- 基地址：`{settings.seaweedfs_base_url}`",
            "- 用途：存储病害图片、定距图片等对象。",
            "- 智能体访问方式：通过 SSH 在服务器上执行 `curl http://127.0.0.1:28888/<path>`，再由后端转发给前端。",
            "- 数据库字段关联：`ai_problem.problem_url`、`ai_asset_main.picture`、`ai_distance_capture.distance_capture_url` 保存对象路径。",
            "- 当前测试数据中多数图片路径为 404 或空，按未来有数据设计。",
            "",
        ]
    )


def _overview(mysql_tables: int, pg_tables: int) -> str:
    databases = [
        row.get("Database") or row.get("DATABASE") or ""
        for row in _rows("mysql", "SHOW DATABASES")
    ]
    return "\n".join(
        [
            "# 数据库字段字典总览",
            "",
            f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
            "",
            "| 数据源 | 定位 | 表数量 |",
            "|---|---|---:|",
            f"| MySQL `{settings.mysql_database}` | 主业务库，只读查询 | {mysql_tables} |",
            f"| PostgreSQL `{settings.pg_database}` | GPS 轨迹库，只读查询 | {pg_tables} |",
            "| SeaweedFS | 图片对象存储，HTTP 读取 | - |",
            "",
            "MySQL 可见数据库：",
            "",
            *[f"- `{name}`" for name in databases],
            "",
            "智能体实际查询范围：",
            "",
            f"- MySQL 主业务库 `{settings.mysql_database}`；",
            f"- PostgreSQL `{settings.pg_database}`；",
            "- SeaweedFS 图片文件。",
            "",
            "所有业务表查询都必须遵守只读、表白名单、`del_flag=0` 和行数上限规则。",
            "",
        ]
    )


def main() -> None:
    mysql_doc = _mysql_dictionary()
    pg_doc = _pg_dictionary()
    seaweedfs_doc = _seaweedfs_dictionary()
    overview = _overview(mysql_doc.count("\n## `"), pg_doc.count("\n## `"))

    dict_dir = settings.knowledge_root / "数据库字段字典"
    dict_dir.mkdir(parents=True, exist_ok=True)
    (dict_dir / "00-总览.md").write_text(overview, encoding="utf-8")
    (dict_dir / "01-MySQL-new_four_road_base_ai.md").write_text(mysql_doc, encoding="utf-8")
    (dict_dir / "02-PostgreSQL-gpsdb.md").write_text(pg_doc, encoding="utf-8")
    (dict_dir / "03-SeaweedFS.md").write_text(seaweedfs_doc, encoding="utf-8")

    root_path = PROJECT_ROOT / "数据库字段字典.md"
    original = root_path.read_text(encoding="utf-8") if root_path.exists() else ""
    marker = "\n\n---\n\n# 附录：全量数据库结构（自动生成）"
    if marker in original:
        original = original.split(marker, 1)[0]
    appendix = (
        marker
        + "\n\n"
        + overview
        + "\n\n"
        + mysql_doc
        + "\n\n"
        + pg_doc
        + "\n\n"
        + seaweedfs_doc
    )
    root_path.write_text(original.rstrip() + appendix + "\n", encoding="utf-8")

    result = sync_dataset("数据库字段字典", "database_dictionary")
    print(
        {
            "mysql_tables": mysql_doc.count("\n## `"),
            "pg_tables": pg_doc.count("\n## `"),
            "knowledge_dir": str(dict_dir),
            "root_file": str(root_path),
            "sync": result,
        }
    )


if __name__ == "__main__":
    main()
