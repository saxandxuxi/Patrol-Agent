"""按"两表之间有对应列"自动发现并补全 Wren 关系（relationships.yml）。

思路（用户提的）：不看业务，直接看**列**——A 表里指向 B 表的列（`routes_id` → `route.id`、
`damage_type` → `damage_dict.problem_code` 这种）在数据上一定是"值基本都落在对方表的那一列里"。
所以每一步都用**只读查询**验证一遍值覆盖率，而不是靠列名猜：

  1. 候选只从"键列"里出：`*_id`（外键命名约定）+ 与目标表键列同名的列；
  2. 逐候选做一次值覆盖检查：拿 A 列里的**去重值**，看有多少能落在 B 的目标列里；
  3. 覆盖率高（默认 ≥95%）且方向明确（子→父）才写进 relationships.yml，注释里带上实测覆盖率；
  4. 已经在文件里的（同一对模型 + 同一条件）跳过。

只读：默认 dry-run 只打印将要追加的条目；加 `--write` 才真的追加到文件末尾（原有内容不动）。

用法：
    python gen_relationships.py                # 只打印
    python gen_relationships.py --write        # 追加写入
    python gen_relationships.py --min-coverage 0.9 --sample 200
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import wren_engine  # noqa: E402
from app.services.db import db_manager  # noqa: E402

KIND = "mysql"
# 这些列在每张表里都有、语义上是"记录自身的属性"，同名不代表两张表有关系
_SHARED_NOISE = {
    "id", "del_flag", "create_time", "update_time", "create_by", "update_by",
    "tenant_id", "remark", "sort", "status", "version", "create_user", "update_user",
}


def _models(kind: str) -> list[dict[str, Any]]:
    mdl = wren_engine._load_mdl(str(wren_engine._project_dir(kind)))
    out: list[dict[str, Any]] = []
    for model in mdl["models"]:
        name = str(model.get("name") or "")
        table = str((model.get("table_reference") or {}).get("table") or "") or str(
            (mdl.get("tables_by_model") or {}).get(name) or ""
        )
        columns = [
            (str(col.get("name") or ""), str(col.get("type") or "").upper())
            for col in model.get("columns") or []
            if col.get("name")
        ]
        if name and table and columns:
            out.append({"model": name, "table": table, "columns": columns, "ref_sql": model.get("ref_sql")})
    return out


def _keys_of(model: dict[str, Any]) -> list[str]:
    """这张表里"可以被别人指向"的列：主键 id + 编码类键列（problem_code / code…）。"""
    keys = []
    for name, _type in model["columns"]:
        low = name.lower()
        if low == "id" or low.endswith("_code") or low in {"problem_code", "code"}:
            keys.append(name)
    return keys


def _candidates(models: list[dict[str, Any]]) -> list[dict[str, str]]:
    """(子列, 目标列) 候选：`*_id` 命名约定 + 与目标键列同名。"""
    found: list[dict[str, str]] = []
    for source in models:
        for target in models:
            if source["model"] == target["model"]:
                continue
            target_keys = _keys_of(target)
            target_by_name = {name.lower(): name for name, _t in target["columns"]}
            for column, _ctype in source["columns"]:
                low = column.lower()
                if low in _SHARED_NOISE:
                    continue
                hit = ""
                if low.endswith("_id") and len(low) > 3:
                    # routes_id → route.id：去掉 _id 的"表名部分"要能对上**目标模型/表名**
                    # （不是对目标的键列名——以前这么写导致这条分支永远不成立，召回为 0）
                    stem = low[:-3]
                    table = target["table"].lower()
                    names = {
                        target["model"].lower(),
                        table,
                        table.replace("basic_facilities_", ""),
                        table.replace("ai_", ""),
                        table.replace("maintenance_", ""),
                    }
                    if any(
                        item and (stem == item or item.endswith(f"_{stem}") or stem.endswith(f"_{item}") or stem.endswith(item))
                        for item in names
                    ):
                        hit = "id"
                if not hit and low in {k.lower() for k in target_keys} and low not in {"id"}:
                    hit = target_by_name.get(low, "")
                if hit:
                    found.append(
                        {"source": source["model"], "source_table": source["table"], "column": column,
                         "target": target["model"], "target_table": target["table"], "target_column": hit}
                    )
    return found


def _coverage(source_table: str, column: str, target_table: str, target_column: str, sample: int) -> float | None:
    """子列的去重值里，有多大比例能在父列里找到（只读，取前 sample 个去重值）。"""
    try:
        rows = db_manager.query(
            KIND,
            f"SELECT DISTINCT `{column}` AS v FROM `{source_table}` "
            f"WHERE `{column}` IS NOT NULL LIMIT {int(sample)}",
        )["rows"]
    except Exception:
        return None
    values = [str(row.get("v")) for row in rows if str(row.get("v") or "").strip()]
    if not values:
        return None
    quoted = ", ".join("'" + item.replace("'", "''") + "'" for item in values[:sample])
    try:
        hit = db_manager.query(
            KIND,
            f"SELECT COUNT(*) AS c FROM (SELECT DISTINCT `{target_column}` AS v FROM `{target_table}` "
            f"WHERE `{target_column}` IN ({quoted})) AS t",
        )["rows"]
    except Exception:
        return None
    return (int(hit[0]["c"]) / len(values)) if hit else None


def _target_unique(table: str, column: str) -> bool | None:
    """目标列在目标表里**唯一**吗？（MANY_TO_ONE 的前提）

    值域对得上**不代表能连**：实测 `ai_problem_base.disease_categories_type` 值域能罩住
    `ai_problem.categories_type`，但字典表 84 行里"路面病害"有 ~50 行——拿它连表会把每条病害
    放大 50 倍，计数全错。所以再加一道闸：目标列必须近似唯一（去重比值 ≥0.98）。
    """
    try:
        rows = db_manager.query(
            KIND,
            f"SELECT COUNT(*) AS total, COUNT(DISTINCT `{column}`) AS uniq FROM `{table}`",
        )["rows"]
    except Exception:
        return None
    if not rows:
        return None
    total, uniq = int(rows[0].get("total") or 0), int(rows[0].get("uniq") or 0)
    if total <= 0:
        return None
    return (uniq / total) >= 0.98


def _existing(path: Path) -> tuple[set[tuple[str, str, str]], set[str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: set[tuple[str, str, str]] = set()
    names: set[str] = set()
    for item in data.get("relationships") or []:
        if item.get("name"):
            names.add(str(item["name"]))
        models = tuple(sorted(str(m) for m in (item.get("models") or [])))
        if len(models) == 2:
            out.add((models[0], models[1], str(item.get("condition") or "").replace(" ", "")))
    return out, names


def _comment_tokens(text: str) -> set[str]:
    """注释切成可比对的词元：英文/数字按词，中文按 2~3 字滑窗（和 wren_engine 同一套思路）。"""
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9_]{2,}", str(text or "").lower()):
        tokens.add(word)
    for chunk in re.findall(r"[\u4e00-\u9fa5]+", str(text or "")):
        for size in (3, 2):
            tokens.update(chunk[i : i + size] for i in range(len(chunk) - size + 1))
    return tokens


# 通用词：光共享这些**不算**语义一致。"路线类型" vs "主体类型" 只共享"类型"，
# "病害代码" vs "路线代码" 只共享"代码"——它们是两码事，不能凭这个连表。
_GENERIC_TOKENS = {
    "类型", "代码", "编码", "编号", "名称", "状态", "是否", "值", "id", "code", "type",
    "属性", "信息", "数据", "标识", "记录", "所属", "对应", "关联",
}


def _comment_overlap(left: str, right: str) -> str:
    """两侧注释最长的**有信息量**的共同词元（≥2 字、不是通用词），没有就返回空串。"""
    shared = (_comment_tokens(left) & _comment_tokens(right)) - _GENERIC_TOKENS
    shared = {item for item in shared if len(item) >= 2}
    return max(shared, key=len) if shared else ""


def _column_comments(kind: str) -> dict[tuple[str, str], str]:
    """(物理表, 列) → 注释（字段目录里的 comment，与前端看到的同一份）。"""
    from app.services import schema_catalog as catalog

    out: dict[tuple[str, str], str] = {}
    for table in catalog.field_catalog(kind).get("tables") or []:
        name = str(table.get("table") or "")
        for column in table.get("columns") or []:
            out[(name, str(column.get("name") or ""))] = str(
                column.get("comment") or column.get("label") or ""
            )
    return out


def _sample_column(table: str, column: str, limit: int) -> set[str]:
    """一列的去重取值样本（只读一次）。值域对撞先在这份样本上做，省掉成百次查询。"""
    try:
        rows = db_manager.query(
            KIND,
            f"SELECT DISTINCT `{column}` AS v FROM `{table}` "
            f"WHERE `{column}` IS NOT NULL LIMIT {int(limit)}",
        )["rows"]
    except Exception:
        return set()
    return {str(row.get("v")).strip() for row in rows if str(row.get("v") or "").strip()}


def _code_like(name: str) -> bool:
    low = name.lower()
    return low.endswith(("_code", "_type")) or low in {"code", "type", "asset_code"}


def _value_candidates(
    models: list[dict[str, Any]], comments: dict[tuple[str, str], str], sample: int
) -> list[dict[str, str]]:
    """**值语义型**关系：列名完全不同（`damage_type` ↔ `problem_code`），只能靠值域连。

    做法：每张表的 code/type 类列各采样一次（≤sample 个去重值），在内存里两两对撞，
    样本重合度高才留下；注释相近（"病害代码"≈"病害代码"）当**辅助证据**记进去。
    最后交给 `_coverage` 用真值确认——采样只是用来把候选从上千个压到几个。
    """
    columns: list[dict[str, Any]] = []
    for model in models:
        for name, ctype in model["columns"]:
            if _code_like(name):
                columns.append({"model": model["model"], "table": model["table"],
                                "column": name, "type": ctype})
    samples: dict[tuple[str, str], set[str]] = {}
    for item in columns:
        samples[(item["table"], item["column"])] = _sample_column(item["table"], item["column"], sample)
    out: list[dict[str, str]] = []
    for source in columns:
        left = samples.get((source["table"], source["column"])) or set()
        # 只要一列有 ≥2 个不同值就够了：实测 `disease.damage_type` 全库只有 2 个值
        # （10/5），门槛定 3 会把"病害类型 ↔ 病害代码"这种真关系直接漏掉。
        if len(left) < 2:
            continue
        for target in columns:
            if target["table"] == source["table"] or not _code_like(target["column"]):
                continue
            right = samples.get((target["table"], target["column"])) or set()
            if len(right) < 2:
                continue
            shared = len(left & right) / min(len(left), len(right))
            if shared < 0.6:
                continue
            comment_same = _comment_overlap(
                comments.get((source["table"], source["column"]), ""),
                comments.get((target["table"], target["column"]), ""),
            )
            # **硬闸**：值域重合只是必要条件。枚举值域小（0/1/2 这种）时两件不相干的事也会
            # "全覆盖"，实测撞出过 `病害基础状态` ↔ `路线类型` 这种假关系。
            # 必须**两边注释也说明是同一个东西**（共享一个有信息量的词，不能只共享"类型/代码"）
            # 才允许连；注释对不上就跳过——哪怕值域 100% 重合。
            if not comment_same:
                continue
            out.append(
                {
                    "source": source["model"], "source_table": source["table"],
                    "column": source["column"], "target": target["model"],
                    "target_table": target["table"], "target_column": target["column"],
                    "reason": f"值域重合 {shared:.0%} + 注释相近（{comment_same}）",
                }
            )
    return out


def main() -> int:
    argv = sys.argv[1:]
    write = "--write" in argv
    min_coverage = 0.95
    sample = 120
    if "--min-coverage" in argv:
        min_coverage = float(argv[argv.index("--min-coverage") + 1])
    if "--sample" in argv:
        sample = int(argv[argv.index("--sample") + 1])

    models = _models(KIND)
    path = Path(wren_engine._project_dir(KIND)) / "relationships.yml"
    known, known_names = _existing(path)
    comments = _column_comments(KIND)
    value_candidates = _value_candidates(models, comments, max(sample, 40))
    name_candidates = _candidates(models)
    print(
        f"模型 {len(models)} 个；已有关系 {len(known)} 条；"
        f"候选 {len(value_candidates)}（值语义）+ {len(name_candidates)}（命名/同名）；逐个验值（只读）…"
    )

    added: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    matched_known = 0
    for item in value_candidates + name_candidates:
        pair = tuple(sorted((item["source"], item["target"])))
        condition = f"{item['source']}.{item['column']} = {item['target']}.{item['target_column']}"
        key = (pair[0], pair[1], condition.replace(" ", ""))
        if key in known:
            matched_known += 1
            print(f"  ≈ 已在文件里：{condition}")
            continue
        dedupe = (item["source"], item["column"] + "→" + item["target"] + "." + item["target_column"])
        if dedupe in seen:
            continue
        seen.add(dedupe)
        ratio = _coverage(item["source_table"], item["column"], item["target_table"], item["target_column"], sample)
        if ratio is None or ratio < min_coverage:
            print(f"  ✗ {condition}  覆盖率 {('—' if ratio is None else f'{ratio:.0%}')}")
            continue
        # 目标列必须唯一：不唯一就会把源表的行放大（实测字典表按大类连会放大 50 倍）
        unique = _target_unique(item["target_table"], item["target_column"])
        if unique is False:
            print(f"  ✗ {condition}  覆盖 {ratio:.0%} 但目标列不唯一（连表会放大行数）")
            continue
        name = f"{item['source']}_{item['target']}"
        used = set(known_names) | {existing["name"] for existing in added}
        if name in used:
            # 同一对表可能有多条关系（都连 damage_dict，但走不同的列）：名字必须唯一，
            # 不然 Wren 认不出是哪一条（实测自动追加时和已有那条撞名了）。
            name = f"{name}_{item['column']}"
        added.append({**item, "name": name, "condition": condition, "coverage": ratio})
        print(f"  ✓ {condition}  覆盖率 {ratio:.0%}  {item.get('reason') or ''}")

    if not added:
        print(f"没有需要新增的关系（其中有 {matched_known} 个候选与文件里已有的一致）。")
        return 0
    block = ["", "# ── 自动发现（gen_relationships.py 用真实数据验过值覆盖率后追加）─────────────"]
    for item in added:
        block += [
            f"  # source: auto 覆盖率 {item['coverage']:.0%}"
            + (f"；{item['reason']}" if item.get("reason") else ""),
            f"- name: {item['name']}",
            "  models:",
            f"  - {item['source']}",
            f"  - {item['target']}",
            "  join_type: MANY_TO_ONE",
            f"  condition: {item['condition']}",
        ]
    text = "\n".join(block) + "\n"
    if write:
        path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n" + text, encoding="utf-8")
        print(f"\n已追加 {len(added)} 条到 {path}")
    else:
        print(f"\n（dry-run）将追加 {len(added)} 条，预览：\n{text}")
        print("确认无误后加 --write 写入。")
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，打印 ✓/✗ 会 UnicodeEncodeError。**只在直接运行时**换编码，
    # 别在 import 时动 sys.stdout（那会把调用方的 stdout 缓冲区一起关掉）。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    raise SystemExit(main())
