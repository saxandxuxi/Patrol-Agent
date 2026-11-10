"""Wren AI 语义层接入（按 docs/WrenAI集成方案.md 的阶段二/三实现）。

分两块能力，彼此独立：

1. **MDL 语义上下文**（`mdl_context`）：直接读 wren_projects/ 下的 MDL YAML，
   把"模型 / 字段 / 指标 / 关系"渲染成给 LLM 看的结构化清单。
   这层**不依赖任何第三方包**，现在就能用——它替代/补强 RAGFlow 的字段字典文本检索。

2. **MDL-SQL 编译**（`compile_mdl_sql`）：把 LLM 写的 MDL-SQL 编译成物理 SQL。
   这层需要 Wren 引擎（Python 包 `wrenai`，要求 Python≥3.11）。
   环境不满足时 `available()` 为 False，调用方自动回退到内置 LLM 写物理 SQL。

执行永远走现有 db_manager（SSH 隧道），Wren 只做编译，不接管连接——方案里的"方式一"。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import settings
from .logging_service import log_error, log_event


def _project_dir(kind: str) -> Path:
    name = "road_mysql" if kind == "mysql" else "road_pg"
    return Path(settings.wren_projects_dir) / name


@lru_cache(maxsize=8)
def _load_mdl(project: str) -> dict[str, Any]:
    """读一个 Wren project 的 MDL（v5：wren_project.yml + models/*/metadata.yml + cubes/*）。"""
    base = Path(project)
    mdl: dict[str, Any] = {
        "models": [],
        "cubes": [],
        "relationships": [],
        "core": set(),
        "tables_by_model": {},
    }
    if not base.exists():
        return mdl
    overrides = base / "overrides.yml"
    if overrides.exists():
        try:
            data = yaml.safe_load(overrides.read_text(encoding="utf-8")) or {}
            for table, item in (data.get("models") or {}).items():
                if isinstance(item, dict) and item.get("core"):
                    mdl["core"].add(str(item.get("name") or table))
                if isinstance(item, dict) and item.get("name"):
                    # 模型名 → 物理表名（ref_sql 模型没有 table_reference，只能从这里反查）
                    mdl["tables_by_model"][str(item["name"])] = str(table)
        except Exception as exc:
            log_error("wren", "overrides_failed", exc, path=str(overrides))
    for path in sorted((base / "models").glob("*/metadata.yml")):
        try:
            mdl["models"].append(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except Exception as exc:
            log_error("wren", "mdl_model_failed", exc, path=str(path))
    for path in sorted((base / "cubes").glob("*/metadata.yml")):
        try:
            mdl["cubes"].append(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        except Exception as exc:
            log_error("wren", "mdl_cube_failed", exc, path=str(path))
    rel = base / "relationships.yml"
    if rel.exists():
        try:
            mdl["relationships"] = (yaml.safe_load(rel.read_text(encoding="utf-8")) or {}).get(
                "relationships", []
            )
        except Exception as exc:
            log_error("wren", "mdl_relations_failed", exc, path=str(rel))
    return mdl


def physical_tables(kind: str, models: set[str]) -> set[str]:
    """选中的模型 → 物理表名（物理 SQL 路径要靠它把 schema 缩小到这几张表）。"""
    if not models:
        return set()
    mdl = _load_mdl(str(_project_dir(kind)))
    tables: set[str] = set()
    for model in mdl["models"]:
        if str(model.get("name")) not in models:
            continue
        table = str((model.get("table_reference") or {}).get("table") or "")
        if not table:
            # 带 ref_sql 的模型（disease/route/section/damage_dict）没有 table_reference，
            # 物理表名在 overrides 里（basic_facilities_routes → route），反向查一次。
            table = (mdl.get("tables_by_model") or {}).get(str(model.get("name")), "")
        if table:
            tables.add(table)
    return tables


def table_list(kind: str, columns_per_table: int = 10) -> str:
    """给选表器看的表清单：模型名 + 业务说明 + **关键列（带中文注释）**。

    为什么不写"用户会怎么叫这张表"的别名表：用户对同一张表的叫法枚举不完（"定距拍
    的片子"、"桩点台账"、"构造物清单"…）。给模型**表的语义证据**（说明 + 列注释），
    让它自己判断；新增/改表都不用改代码（表结构变了同步 MDL 即可）。
    """
    mdl = _load_mdl(str(_project_dir(kind)))
    lines: list[str] = []
    for model in mdl["models"]:
        name = str(model.get("name") or "")
        if not name:
            continue
        desc = str((model.get("properties") or {}).get("description", "")).rstrip("。").strip()
        table = (model.get("table_reference") or {}).get("table", "")
        # 关键列（带中文注释）就是"表语义"的最好证据：选表器据此判断用户说的是哪张表
        labelled = []
        for column in model.get("columns") or []:
            column_name = str(column.get("name") or "")
            note = str((column.get("properties") or {}).get("description") or "").split("（")[0].strip()
            if not column_name or not note:
                continue
            labelled.append(f"{column_name}({note[:14]})")
            if len(labelled) >= columns_per_table:
                break
        columns_text = "、".join(labelled)
        lines.append(
            f"- {name}｜{desc[:80] or '无说明'}"
            f"{'｜物理表 ' + table if table else ''}"
            f"{'｜列：' + columns_text if columns_text else ''}"
        )
    return "\n".join(lines)


@lru_cache(maxsize=8)
def _schema_index(project: str) -> tuple[dict[str, Any], ...]:
    """表语义索引：每张表的说明 + 每列的列名/中文注释（全部来自 MDL，不手写）。

    这是"用户叫法穷举不完"的解法：不枚举叫法，而是把**表自己的语义**做成可检索的
    索引。库结构变了 → 重建 MDL → 索引自动跟着变。
    """
    mdl = _load_mdl(project)
    tables_by_model = mdl.get("tables_by_model") or {}
    cards: list[dict[str, Any]] = []
    for model in mdl["models"]:
        name = str(model.get("name") or "")
        if not name:
            continue
        table = str((model.get("table_reference") or {}).get("table") or "")
        if not table:
            table = str(tables_by_model.get(name) or "")
        columns: list[tuple[str, str]] = []
        for column in model.get("columns") or []:
            column_name = str(column.get("name") or "")
            if not column_name:
                continue
            note = str((column.get("properties") or {}).get("description") or "").strip()
            columns.append((column_name, note))
        cards.append(
            {
                "name": name,
                "table": table,
                "description": str((model.get("properties") or {}).get("description") or "").strip(),
                "columns": tuple(columns),
            }
        )
    return tuple(cards)


def _terms(text: str) -> set[str]:
    """把一段话切成可比对的词元：英文/数字按词切，中文按 2~3 字滑窗切。

    不引分词库：中文滑窗对"表名/列注释"这种短语的召回够用，而且对未登录词（用户自己
    发明的叫法）同样有效。
    """
    lowered = str(text or "").lower()
    tokens: set[str] = {
        token
        for token in re.findall(r"[a-z0-9_]{2,}", lowered)
        if not token.isdigit()
    }
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        for size in (2, 3):
            if len(chunk) < size:
                continue
            for index in range(len(chunk) - size + 1):
                tokens.add(chunk[index : index + size])
    return tokens


@lru_cache(maxsize=8)
def _rare_chars(project: str) -> frozenset[str]:
    """在整个库里只出现在少数几张表上的单字（"桥""坡""隧""裂"…）。

    中文两字滑窗漏掉单字概念时（"有哪些桥" → 没有"桥梁"这个词），靠这些**罕见单字**
    兜一把；"路""表"这种满库都有的字不进来，免得把无关表都拉起来。
    """
    counter: dict[str, int] = {}
    for card in _schema_index(project):
        text = card["description"] + card["table"]
        for column_name, note in card["columns"]:
            text += column_name + note
        for char in set(re.findall(r"[\u4e00-\u9fff]", text)):
            counter[char] = counter.get(char, 0) + 1
    return frozenset(char for char, count in counter.items() if count <= 3)


def search_models(kind: str, text: str, limit: int = 4) -> list[dict[str, Any]]:
    """按**表语义**检索候选模型：用户的原话 → 最可能是哪几张表。

    打分依据（全部来自 MDL，不手写别名）：表说明、物理表名、列名、列注释。命中的证据
    会一起返回，交给选表模型做最终语义判定——代码只负责"把证据摆出来"。
    """
    query = _terms(text)
    if not query:
        return []
    rare_query_chars = {
        char for char in re.findall(r"[\u4e00-\u9fff]", str(text or "")) if char in _rare_chars(str(_project_dir(kind)))
    }
    scored: list[dict[str, Any]] = []
    for card in _schema_index(str(_project_dir(kind))):
        # 第三项 = 是否"表级证据"（表名/表说明）：判断"这句话问的是哪张表"时它比
        # 某个列的注释更算数（"创建时间""设备编号"这种通用列满库都是）。
        evidence: list[tuple[str, float, bool]] = []
        if card["description"]:
            evidence.append(("表说明：" + card["description"], 2.0, True))
        if card["table"]:
            evidence.append((card["table"], 2.0, True))
        for column_name, note in card["columns"]:
            if note:
                evidence.append((f"{column_name}（{note}）", 1.0, False))
            else:
                evidence.append((column_name, 0.5, False))
        score = 0.0
        table_score = 0.0
        hits: list[str] = []
        for raw, weight, is_table in evidence:
            overlap = query & _terms(raw)
            if not overlap and not any(char in raw for char in rare_query_chars):
                continue
            # 命中词元越长，证据越强；用长度平方加权，避免"病害"这种到处都有的两字词
            # 把无关表顶上来。
            gain = sum(len(token) ** 2 for token in overlap) / 4.0
            gain += 0.4 * sum(1 for char in rare_query_chars if char in raw)
            score += weight * gain
            if is_table:
                table_score += weight * gain
            if len(hits) < 3:
                hits.append(raw[:60])
        if score <= 0:
            continue
        scored.append(
            {
                "name": card["name"],
                "score": round(score, 2),
                "table_score": round(table_score, 2),
                "hits": hits,
            }
        )
    scored.sort(key=lambda item: (-item["score"], item["name"]))
    return scored[:limit]


def suggest_fields(kind: str, terms: list[str], limit: int = 6) -> list[str]:
    """模型写了一个不存在的列名时，按列名/列注释反查"它想写的很可能是哪个字段"。"""
    wanted: list[tuple[str, float]] = []
    for term in terms:
        lowered = str(term or "").strip().lower()
        if not lowered:
            continue
        wanted.append((lowered, 3.0))
        # 编出来的列名多半是英文拼接（route_length_km）：拆开再匹配一次，
        # 否则整串在真实列名里根本不存在，永远给不出建议。
        wanted.extend((part, 1.0) for part in lowered.split("_") if len(part) >= 4)
    if not wanted:
        return []
    scored: list[tuple[float, str]] = []
    for card in _schema_index(str(_project_dir(kind))):
        for column_name, note in card["columns"]:
            lowered = column_name.lower()
            haystack = f"{lowered} {note.lower()}"
            score = 0.0
            for term, weight in wanted:
                if lowered == term:
                    score = max(score, weight)
                elif term in lowered:
                    score = max(score, weight * 0.9)
                elif term in note.lower():
                    score = max(score, weight * 0.8)
                elif lowered in term:
                    score = max(score, weight * 0.7)
            label = f"{card['name']}.{column_name}" + (f"（{note[:24]}）" if note else "")
            if score > 0:
                scored.append((score, label))
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    out: list[str] = []
    for _score, label in scored:
        if label not in out:
            out.append(label)
        if len(out) >= limit:
            break
    return out


def related_models(
    kind: str, names: set[str], hops: int = 1, limit: int = 6
) -> set[str]:
    """把选中的模型按关系扩一圈：选了 disease 就必须带上 route/damage_dict/patrol，
    否则模型写 `JOIN route` 时提示词里根本没这张表（Wren 不会自动补 JOIN）。

    limit：最多渲染几张。避免"随便选一张就把 17 张表全拉回来"——
    以前闭包没有上限，"路线等级"这种只要 route 的问题也被撑成 6~7 张，选表就白做了。
    """
    mdl = _load_mdl(str(_project_dir(kind)))
    known = {str(model.get("name")) for model in mdl["models"]}
    seeds = {name for name in names if name in known}
    if not seeds:
        return set()
    # 只从**选中的种子**往外扩，而且**沿 JOIN 方向**扩：
    # 条件写成 `disease.routes_id = route.id` 时，disease 需要 route 来 JOIN；
    # 反过来 route 不需要 disease（"路线等级多少"不该把病害表拉进来）。
    # 以前是双向传递闭包，问什么都撑成同一套 6~7 张表，选表就白做了。
    chosen = set(seeds)
    for relation in mdl["relationships"]:
        raw = relation.get("models")
        # 关系里的 models 是列表（[disease, route]），也兼容 "disease, route" 这种写法
        if isinstance(raw, (list, tuple)):
            pair = [str(item).strip() for item in raw]
        else:
            text = str(raw or "").strip().strip("[]")
            pair = [item.strip().strip("'\"") for item in text.split(",") if item.strip()]
        endpoints = [item for item in pair if item in known]
        if len(endpoints) < 2:
            continue
        condition = str(relation.get("condition") or "")
        # 条件左边是"引用方"，右边是"被引用的表"；只有种子在左边时才补右边。
        left = condition.split("=")[0].strip().split(".")[0].strip() if "=" in condition else ""
        target = endpoints[-1]
        if left and left in seeds:
            chosen.add(target)
    if len(chosen) <= limit:
        return chosen
    # 超了就先保"选中的"，再按 core 优先补关系表（core 是最常用的业务模型）
    core = mdl.get("core") or set()
    extra = sorted(chosen - names, key=lambda item: (item not in core, item))
    kept = set(names)
    for item in extra:
        if len(kept) >= limit:
            break
        kept.add(item)
    return kept


def mdl_context(
    kind: str, models: set[str] | None = None, focus: set[str] | None = None
) -> str:
    """把 MDL 渲染成提示词用的紧凑文本（模型/字段/指标/关系）。"""
    mdl = _load_mdl(str(_project_dir(kind)))
    if not mdl["models"]:
        return ""
    core: set[str] = mdl.get("core") or set()
    lines: list[str] = [
        "可用业务模型（MDL，优先依据；引用模型名/字段名，不要写物理表名）。"
        "带字段说明的是常用模型，其余只列字段名（写法一样）："
    ]
    for model in mdl["models"]:
        name = str(model.get("name"))
        # 两步走第二步：只渲染选中的表（+ 关系闭包），而不是把 17 张表一次性塞进去
        # （塞不完会被 [:12000] 截断，后面的表和字段直接消失——冷门字段问不出来就是这个原因）。
        if models is not None and name not in models:
            continue
        desc = str((model.get("properties") or {}).get("description", "")).rstrip("。").strip()
        table = (model.get("table_reference") or {}).get("table", "")
        # focus=本轮选表结果：即使不是 core 模型也展开字段明细，让"选表"真正作用到
        # 写 SQL 的模型身上；但整张表清单仍在，选漏一张表也不会直接写不出来。
        detail = name in core or name in (focus or set()) or not core
        lines.append(
            f"- {name}（{desc[:120]}；物理表 {table or '见字段定义'}）"
            if detail
            else f"- {name}（{desc[:50]}）"
        )
        if detail:
            columns = []
            for column in model.get("columns") or []:
                label = (column.get("properties") or {}).get("description") or ""
                mark = "（计算字段）" if column.get("is_calculated") else ""
                columns.append(f"{column.get('name')}{mark}{f'：{label}' if label else ''}")
            if columns:
                lines.append("    字段：" + "；".join(columns))
        else:
            names = [str(column.get("name")) for column in model.get("columns") or []]
            if names:
                shown = "、".join(names[:40])
                more = f"…（共 {len(names)} 列）" if len(names) > 40 else ""
                lines.append(f"    字段：{shown}{more}")
    for cube in mdl["cubes"]:
        measures = "、".join(
            f"{m.get('name')}（{(m.get('properties') or {}).get('description', '')}）"
            for m in cube.get("measures") or []
        )
        dims = "、".join(
            f"{d.get('name')}（{(d.get('properties') or {}).get('description', '')}）"
            for d in cube.get("dimensions") or []
        )
        lines.append(f"- 指标cube {cube.get('name')}（基于 {cube.get('base_object')}）：指标 {measures}；维度 {dims}")
    if mdl["relationships"]:
        # 实测：Wren 不支持"隐式关系引用"——只写 WHERE route.short_name = 'x' 不写 JOIN 时，
        # dry_plan 会"编译成功"但产物里没有 JOIN，执行时直接报 Unknown column 'route.short_name'。
        lines.append(
            "关系（**必须显式写 JOIN**，不能在 WHERE 里直接引用别的模型字段）："
            + "；".join(
                f"{r.get('models')} {r.get('condition')}" for r in mdl["relationships"]
            )
        )
        lines.append(
            "JOIN 写法示例：FROM disease d JOIN route r ON d.routes_id = r.id "
            "JOIN damage_dict dd ON d.problem_base_id = dd.id"
        )
    return "\n".join(lines)


_engine_cache: dict[str, Any] = {}


def clear_caches() -> None:
    """MDL 重新生成 / 重新 build 之后必须调用，否则进程内还是旧的 MDL 和旧引擎。"""
    _load_mdl.cache_clear()
    _schema_index.cache_clear()
    _engine_cache.clear()


def available(kind: str = "mysql") -> bool:
    """Wren 引擎是否可用（需要 Python≥3.11 且装了 wrenai）。"""
    if not settings.wren_enabled:
        return False
    try:
        import wren  # noqa: F401
    except Exception:
        return False
    return True


_WREN_DATA_SOURCE = {"mysql": "mysql", "postgresql": "postgres"}


def _engine(kind: str) -> Any:
    if kind in _engine_cache:
        return _engine_cache[kind]
    import base64

    from wren import WrenEngine  # type: ignore

    # WrenEngine 收的是 base64 的 mdl.json（跟 wren CLI 的 --mdl 一样），
    # connection_info 传空 dict = 只做编译（transpile-only），执行仍然走 db_manager。
    mdl_path = _project_dir(kind) / "target" / "mdl.json"
    manifest_str = base64.b64encode(mdl_path.read_bytes()).decode()
    engine = WrenEngine(manifest_str, _WREN_DATA_SOURCE.get(kind, "mysql"), {})
    _engine_cache[kind] = engine
    return engine


_UNKNOWN_FIELD = re.compile(r"No field named ([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_UNKNOWN_TABLE = re.compile(r"table '([^']+)' not found")
# 只取 from/join 后面的表名（不含子查询/别名），用来判"这是不是物理 SQL"
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([`\"\[]?[A-Za-z_][\w.]*[`\"\]]?)", re.IGNORECASE)
_JOIN_CLAUSE = re.compile(
    r"\bjoin\s+([A-Za-z_]\w*)"
    r"(?:\s+(?:as\s+)?(?!on\b)([A-Za-z_]\w*))?"
    r"\s+on\s+(.+?)"
    r"(?=\b(?:left|right|inner|outer|cross|straight_join|join)\b|"
    r"\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_MODEL_REF = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_]\w*)"
    r"(?:\s+(?:as\s+)?(?!where\b|on\b|group\b|order\b|having\b|limit\b)([A-Za-z_]\w*))?",
    re.IGNORECASE,
)
_QUALIFIED_EQUALITY = re.compile(
    r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*"
    r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b",
    re.IGNORECASE,
)
# 同一条"跳过编译"的原因只记一次：隧道断的时候每条 SQL 都走这条路，记满日志没用
_SKIP_LOGGED: set[str] = set()


def model_names(kind: str) -> set[str]:
    """这个 Wren 项目里真实存在的模型名（用来挡住跨库/臆造模型的 SQL）。"""
    return {str(model.get("name")) for model in _load_mdl(str(_project_dir(kind)))["models"]}


def physical_table_names(kind: str) -> set[str]:
    """本项目模型背后的物理表名（含去掉库名限定后的短名）。"""
    mdl = _load_mdl(str(_project_dir(kind)))
    names: set[str] = set()
    for model in mdl["models"]:
        table = str((model.get("table_reference") or {}).get("table") or "")
        if not table:
            table = str((mdl.get("tables_by_model") or {}).get(str(model.get("name"))) or "")
        if table:
            names.add(table)
            names.add(table.split(".")[-1])
    return names


def _relationship_columns(kind: str) -> dict[frozenset[str], set[frozenset[tuple[str, str]]]]:
    """模型对 → 合法字段对。只读取 MDL 关系定义，不在代码里再维护一份白名单。"""
    mdl = _load_mdl(str(_project_dir(kind)))
    allowed: dict[frozenset[str], set[frozenset[tuple[str, str]]]] = {}
    for relation in mdl["relationships"]:
        models = [str(item) for item in (relation.get("models") or []) if item]
        if len(models) != 2:
            continue
        match = _QUALIFIED_EQUALITY.search(str(relation.get("condition") or ""))
        if not match:
            continue
        left_model, left_column, right_model, right_column = (
            str(item).lower() for item in match.groups()
        )
        if {left_model, right_model} != set(models):
            continue
        pair = frozenset({left_model, right_model})
        allowed.setdefault(pair, set()).add(
            frozenset({(left_model, left_column), (right_model, right_column)})
        )
    return allowed


def join_contract_issue(kind: str, mdl_sql: str) -> str:
    """检查 MDL-SQL 的每个跨模型 JOIN 是否符合 relationships.yml。

    Wren 的 dry_plan 能接受字段类型相同的任意等值条件，因此
    `patrol.unit_id = route.id` 也会“编译成功”。这里在执行前补上语义契约：
    两个模型必须存在已声明关系，且 ON 条件里的字段对必须与关系一致。
    """
    text = str(mdl_sql or "")
    if not text or not re.search(r"\bjoin\b", text, re.IGNORECASE):
        return ""
    known_models = model_names(kind)
    aliases: dict[str, str] = {}
    for match in _MODEL_REF.finditer(text):
        model = str(match.group(1) or "").lower()
        if model not in known_models:
            continue
        alias = str(match.group(2) or model).lower()
        aliases.setdefault(alias, model)
        aliases.setdefault(model, model)

    allowed = _relationship_columns(kind)
    for join in _JOIN_CLAUSE.finditer(text):
        target_model = str(join.group(1) or "").lower()
        if target_model not in known_models:
            continue
        target_alias = str(join.group(2) or target_model).lower()
        aliases.setdefault(target_alias, target_model)
        condition = str(join.group(3) or "")
        for left_alias, left_column, right_alias, right_column in _QUALIFIED_EQUALITY.findall(
            condition
        ):
            left_model = aliases.get(left_alias.lower())
            right_model = aliases.get(right_alias.lower())
            if not left_model or not right_model or left_model == right_model:
                continue
            relation_pair = frozenset({left_model, right_model})
            column_pair = frozenset(
                {
                    (left_model, left_column.lower()),
                    (right_model, right_column.lower()),
                }
            )
            if column_pair in allowed.get(relation_pair, set()):
                continue
            observed = (
                f"{left_model}.{left_column.lower()} = "
                f"{right_model}.{right_column.lower()}"
            )
            return (
                f"非法 JOIN：{observed} 不在 MDL 关系定义中。"
                f"请按 relationships.yml 使用已声明的关联字段，不要猜外键。"
            )
    return ""


def references_physical_table(kind: str, sql: str) -> bool:
    """SQL 里是否引用了本项目的物理表（用来认"这本来就是物理 SQL"）。"""
    text = str(sql or "").lower()
    if not text:
        return False
    return any(name.lower() in text for name in physical_table_names(kind) if name)


def columns_of(kind: str, model: str) -> list[str]:
    """某个模型的真实列清单（编译报错时回喂给模型重写用）。"""
    for card in _schema_index(str(_project_dir(kind))):
        if card["name"] == model:
            return [name for name, _note in card["columns"]]
    return []


def compile_error_hint(kind: str, mdl_sql: str, error: str) -> str:
    """把 Wren 的编译报错翻成"能拿去重写"的提示（报告 P1-8/P1-9）。"""
    unknown = _UNKNOWN_FIELD.search(error or "")
    if unknown:
        model, field = unknown.group(1), unknown.group(2)
        columns = columns_of(kind, model)
        if columns:
            return (
                f"Wren 编译报错：模型 {model} 没有字段 {field}。"
                f"{model} 的真实字段是：{'、'.join(columns)}（请改用其中之一，不要自己造列名）"
            )
        return f"Wren 编译报错：模型 {model} 没有字段 {field}（请只用字段清单里存在的字段）"
    table = _UNKNOWN_TABLE.search(error or "")
    if table:
        return (
            f"Wren 编译报错：{table.group(1)} 不是本项目的模型。"
            f"本项目只能用：{'、'.join(sorted(model_names(kind)))}"
        )
    return ""


def dry_plan_mdl_sql(
    kind: str,
    mdl_sql: str,
) -> tuple[str | None, str]:
    """Call Wren once and return only physical SQL plus any raw error text."""
    try:
        physical = _engine(kind).dry_plan(mdl_sql)
    except Exception as exc:
        return None, str(exc)
    if isinstance(physical, dict):
        physical = physical.get("sql") or physical.get("dialect_sql") or ""
    physical = str(physical or "").strip()
    return (physical or None), ""


def compile_mdl_sql_verbose(kind: str, mdl_sql: str) -> tuple[str | None, str]:
    """同 compile_mdl_sql，但把失败原因一起返回（给重写用）。"""
    if not settings.wren_enabled or not mdl_sql:
        return None, ""
    # 喂给引擎的必须是**本项目的 MDL-SQL**，否则引擎只会在 stderr 打一堆噪音：
    #   · 物理 SQL（`new_four_road_base_ai.ai_problem` 这种库表名）→ `table 'wren.…' not found`
    #   · 归一化 SQL（还带 `{route_id}` 占位符）→ `ParserError("Expected: :, found: }")`
    # 两种都在真机上刷屏过。这里提前判定并返回原因，不再把噪声留给引擎（报告 P1-9）。
    if re.search(r"\{[a-z_][a-z0-9_]*\}", mdl_sql):
        return None, "还是归一化 SQL（带 {占位符}），先填槽再编译"
    names = model_names(kind)
    referenced = {token for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", mdl_sql)}
    if names and not (referenced & names):
        return None, f"SQL 里没有本项目的模型（可用：{'、'.join(sorted(names))}），也没引用别的模型"
    # 物理 SQL（直接 from/join 物理表）不能喂给引擎：引擎会按 MDL 去解析这些表名，然后
    # 拿不到数据源目录、刷一屏 `Failed to create logical plan: table 'wren.<库>.<表>' not found`
    # （实测隧道一断就这样：physical_sql_of 里 EXPLAIN 失败 → 退回这里编译物理 SQL）。
    # 只认**非模型名**的物理表：几个模型名和物理表名同名（basic_facilities_bridges…），
    # 那种是正常 MDL-SQL，不能拦。
    tables = {
        match.group(1).strip('`"[] ').split(".")[-1].lower()
        for match in _TABLE_REF.finditer(mdl_sql)
    }
    physical_only = {name.lower() for name in physical_table_names(kind)} - {
        name.lower() for name in names
    }
    stray = tables & physical_only
    if stray:
        key = f"{kind}:{'、'.join(sorted(stray))}"
        if key not in _SKIP_LOGGED:
            _SKIP_LOGGED.add(key)
            log_event(
                "wren",
                "INFO",
                "compile_skipped_physical_sql",
                kind=kind,
                tables="、".join(sorted(stray)),
            )
        return None, f"这是物理 SQL（引用了物理表 {'、'.join(sorted(stray))}），不是 MDL-SQL"
    join_issue = join_contract_issue(kind, mdl_sql)
    if join_issue:
        log_event("wren", "WARNING", "join_contract_rejected", kind=kind, reason=join_issue[:200])
        return None, join_issue
    physical, error = dry_plan_mdl_sql(kind, mdl_sql)
    if error:
        log_event("wren", "INFO", "compile_failed", kind=kind, reason=error[:200])
        return None, compile_error_hint(kind, mdl_sql, error) or error[:200]
    if not physical:
        return None, ""
    log_event("wren", "INFO", "compile_success", kind=kind, mdl_sql=mdl_sql[:120])
    return physical, ""


def compile_mdl_sql(kind: str, mdl_sql: str) -> str | None:
    """MDL-SQL → 物理 SQL。不可用/失败都返回 None（调用方回退到内置 LLM）。"""
    physical, _error = compile_mdl_sql_verbose(kind, mdl_sql)
    return physical
