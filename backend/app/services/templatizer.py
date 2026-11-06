"""成功 SQL 自动模板化（L1，docs/RAG示例直达与AI直答旁路方案.md §二）。

只做三件事，全部离线：

1. **列驱动挖槽**：用 sqlglot 把一条"已验证跑通"的成功 SQL 解析成 AST，把
   WHERE 顶层 AND 里的字面值按**它所在的列**归类成带类型的槽位（路线 / 病害类型 /
   枚举 / 时间 / 数值阈值 / LIMIT），并生成参数化 SQL。结构（表、JOIN、WHERE 逻辑、
   聚合、GROUP BY、投影）一个都不动。
2. **跨样本确认**：按结构指纹聚类，只有"跨样本取过不同值"的位置才开成槽，
   簇内始终同值的位置写进 `match.fixed`；只见过一条的模板记为 `candidate`，不进对话推荐。
3. **回环验证**：用原始值回填后比对结构 + 实跑结果，一致才入库，否则丢弃并写日志。

问答时由 `capabilities` 收成候选卡片交用户确认——**这里从不自动执行任何 SQL**。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from . import preset_lib, schema_catalog
from ..config import settings
from .db import DatabaseError, db_manager
from .logging_service import log_error, log_event
from .resource_loader import load_capability_file

_FILE = Path(__file__).resolve().parents[1] / "data" / "auto_templates.json"
_VERSION: tuple[float, int] | None = None
_CACHE: dict[str, Any] = {}
_LOCK = threading.Lock()

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")
_MONTH_ONLY = re.compile(r"^\d{4}-\d{2}$")
_TOKEN = "zzslotxx{}xxzz"

# 参数标签强制说人话：列名/列注释（routes_id、problem_type、解析桩号米数）不是给用户看的，
# 同一列出现两个槽（time_start / time_end 都叫"病害采集时间"）也必须能区分。
_COLUMN_LABELS = {
    "routes_id": "路线",
    "route_id": "路线",
    "problem_type": "病害类型",
    "problem_code": "病害类型",
    "damage_type": "病害类型",
    "pile_distance": "桩号（米）",
    "pile_number": "桩号",
}


def _cfg() -> dict[str, Any]:
    return load_capability_file("predicates.yaml", {}) or {}


# ── 注册表读写（自带 mtime/size 热加载，后端改文件不用重启）────────────────────
def load(force: bool = False) -> dict[str, Any]:
    global _VERSION, _CACHE
    try:
        stat = _FILE.stat()
        version = (stat.st_mtime, stat.st_size)
    except OSError:
        version = None
    if force or _VERSION != version or not _CACHE:
        if version is None:
            _CACHE = {"version": 1, "templates": []}
        else:
            try:
                _CACHE = json.loads(_FILE.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # noqa: BLE001
                log_error("templatize", "registry_parse_failed", exc, file=str(_FILE))
                _CACHE = {"version": 1, "templates": []}
        from .sql_engine import has_legacy_damage_type_join, normalize_damage_code_joins

        for item in _CACHE.get("templates") or []:
            sql = str(item.get("sql") or "")
            fixed = normalize_damage_code_joins(sql)
            if fixed != sql:
                item["sql"] = fixed
            if has_legacy_damage_type_join(fixed):
                item["enabled"] = False
                item["status"] = "candidate"
                item["review_reason"] = (
                    "旧 damage_type/problem_code 关联已停用；"
                    "病害类型必须按 problem_base_id 重新沉淀"
                )
        _VERSION = version
    return _CACHE


def _write(templates: list[dict[str, Any]], mined: dict[str, Any] | None = None) -> None:
    global _VERSION, _CACHE
    payload = {"version": 1, "generated_at": _now(), "templates": templates}
    # 挖掘审计：保留上一次重挖时"没沉淀成模板"的原因（维护页可见）
    previous = _CACHE if isinstance(_CACHE, dict) else {}
    payload["rejects"] = list(previous.get("rejects") or [])
    # "已挖掘台账"：哪条问答对已经尝试过（成了模板 / 什么原因没成）。
    # 有了它，"模板刷新"才能只处理新加进来的问答对，不重复跑旧的那一批
    # （一次全量挖掘要把每条 SQL 重新解析 + 实跑比对，代价不低）。
    payload["mined"] = dict(mined if mined is not None else previous.get("mined") or {})
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _VERSION = None
    load(force=True)


def _write_rejects(items: list[dict[str, Any]]) -> None:
    """记录"为什么某条成功问答没变成模板"（接口 GET /templates/auto/rejects）。"""
    payload = load(force=True)
    payload["rejects"] = items[:50]
    _FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    load(force=True)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _fill_for_profile(template: dict[str, Any]) -> str:
    """把模板 SQL 里的占位符换成"有代表性的取值"，好让 `_ast_profile` 能解析出结构。

    取值本身不参与 shape/group_by/metrics 判定，**类型对**就行：比较符得能当运算符解析、
    `top_n` 得是一个白名单以内的数（挖槽时它就是数字，被换成了占位符）。
    """
    text = str(template.get("sql") or "")
    for name, spec in (template.get("params") or {}).items():
        kind = str(spec.get("type"))
        if kind == "op":
            value = ">"
        elif name == "top_n" or kind == "int":
            value = str((spec.get("range") or [5])[0])
        elif kind == "numeric":
            value = "1"
        elif kind == "time":
            value = "2026-01-01 00:00:00"
        elif kind == "enum" and spec.get("options"):
            # 静态体检要拿一个真实合法编码，不能统一塞 "x"，否则会把合法枚举槽误报成脏值。
            value = str((spec["options"][0] or {}).get("value") or "x")
        else:
            value = "x"
        text = text.replace("{" + name + "}", value)
    return text


def profile_of_sql(template: dict[str, Any]) -> dict[str, Any] | None:
    """重算一条模板的 SQL 自画像（解析不出来返回 None）。"""
    text = _fill_for_profile(template)
    if "{" in text or "zzslotxx" in text:
        return None
    try:
        tree = sqlglot.parse_one(text, read="mysql")
    except Exception:  # noqa: BLE001
        return None
    return _ast_profile(tree)


def recalibrate_specs(dry_run: bool = True) -> list[dict[str, Any]]:
    """按**当前**的 SQL 自画像重算每条自动模板的 spec（不重挖 SQL、不动条件与槽位）。

    只改 `match` 里的 shapes / subjects / group_by / metrics——`conditions` / `fixed` /
    `params` 是槽位挖掘的产物，重算它们等于改口径，风险不对等。

    为什么需要它：spec 是"挖掘那一刻"按当时的 `_ast_profile` 算好、存进 JSON 的，**之后挖掘器
    修好了识别能力，存量模板不会跟着变**。实测就有一条（"哪几种病害类型最常见"）因为
    `problem_type_name` 当时没被认成病害类型维度，被记成"无分组的计数"，从此再也匹配不上
    （docs/模板spec与现行语义漂移核查.md §3.1/§四.3）。

    返回逐条报告：`updated`（改了，附前后对比）/ `unchanged` / `skipped`（SQL 解析不出来）。
    `dry_run=True` 只看不改。
    """
    templates = list_templates()
    report: list[dict[str, Any]] = []
    dirty = False
    for template in templates:
        entry: dict[str, Any] = {
            "id": str(template.get("id")),
            "question": str(template.get("question") or "")[:40],
            "status": str(template.get("status") or ""),
        }
        fresh = profile_of_sql(template)
        if not fresh:
            entry["action"] = "skipped"
            entry["reason"] = "SQL 解析不出来（占位符没替干净/结构太复杂）"
            report.append(entry)
            continue
        spec = template.setdefault("match", {})
        after = {
            "shapes": [str(item) for item in fresh.get("shapes") or []],
            "subjects": [str(item) for item in fresh.get("subjects") or []],
            "group_by": [str(item) for item in fresh.get("group_by") or []],
            "metrics": sorted({str(item) for item in fresh.get("metrics") or []}),
        }
        before = {
            "shapes": [str(item) for item in spec.get("shapes") or []],
            "subjects": [str(item) for item in spec.get("subjects") or []],
            "group_by": [str(item) for item in spec.get("group_by") or []],
            "metrics": sorted({str(item) for item in spec.get("metrics") or []}),
        }
        if before == after:
            entry["action"] = "unchanged"
            report.append(entry)
            continue
        entry["action"] = "updated"
        entry["before"] = before
        entry["after"] = after
        report.append(entry)
        if not dry_run:
            spec["shapes"] = after["shapes"]
            spec["subjects"] = after["subjects"]
            spec["group_by"] = after["group_by"]
            spec["metrics"] = after["metrics"]
            dirty = True
    if dirty and not dry_run:
        _write(templates)
        log_event(
            "templatize",
            "INFO",
            "specs_recalibrated",
            updated=sum(1 for item in report if item["action"] == "updated"),
        )
    return report


def remine_sql(
    template_id: str, dry_run: bool = True, hints: list[str] | None = None
) -> dict[str, Any]:
    """按**当前**挖掘器重挖一条存量模板的槽位（不动问法、不重跑挖掘台账）。

    为什么需要它：挖掘器修好了，存量记录不会跟着变。实测"看交安事件的病害样图"这条是旧版挖的
    ——`disease_categories_type = '15'` 被当成固定值，维护页显示"（无可换参数）"；
    现在的挖槽器对同一条 SQL 能挖出带 4 个选项的枚举参数。
    `recalibrate_specs` 只管 shapes/主体/分组/指标（它明确不动 params），所以补这一条。

    安全性（只换"更会挖"的，不换口径）：先把旧模板回填成**原始 SQL**，用当前挖掘器重挖，
    只有**参数严格变多**、且用新参数回填后与原始 SQL **逐字等价**（去空白后）才写回；
    写回时清掉旧审核结论（SQL 变了，旧结论作废）。
    """
    template = find(template_id)
    if not template:
        return {"ok": False, "reason": "没有这条模板"}
    old_params = dict(template.get("params") or {})
    sample_values: dict[str, str] = {}
    samples = template.get("samples") or []
    if samples:
        for key, value in (samples[0].get("values") or {}).items():
            sample_values[str(key)] = str(value)
    for name in old_params:
        if name not in sample_values:
            sample_values[name] = ""
    original, note = bind(template, sample_values)
    if not original:
        return {"ok": False, "reason": f"旧模板回填失败：{note}"}
    spec, reason = mine_sql(
        str(template.get("question") or ""), original, "mysql", hints=hints
    )
    if not spec:
        return {"ok": False, "reason": f"重挖没有挖出更细的槽位：{reason}"}
    new_params = dict(spec.get("params") or {})
    # 修复可能不是"多一个槽"，而是"把同一处写死的值换成已有的槽"（"一头还是两头"那条：
    # 外层 `{route_id}`、子查询里写死同一个 id）——所以看**占位符个数**，不能只看参数个数。
    old_marks = len(re.findall(r"\{[a-zA-Z_0-9]+\}", str(template.get("sql") or "")))
    new_marks = len(re.findall(r"\{[a-zA-Z_0-9]+\}", str(spec.get("sql") or "")))
    stale_fixed = set((template.get("match") or {}).get("fixed") or {}) & set(new_params)
    if len(new_params) <= len(old_params) and new_marks <= old_marks and not stale_fixed:
        return {"ok": False, "reason": "重挖没有挖出更多槽位（保持原样）"}
    filled = str(spec.get("sql") or "")
    for name, value in (spec.get("values") or {}).items():
        filled, error = _replace_param(
            filled, str(name), new_params.get(str(name)) or {}, str(value), _cfg()
        )
        if error:
            return {"ok": False, "reason": f"回填校验失败：{error}"}
    def _flat(text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()

    if _flat(filled) != _flat(original):
        return {"ok": False, "reason": "重挖后的 SQL 与原来不等价（保守不替换）"}
    report = {
        "ok": True,
        "applied": not dry_run,
        "id": str(template_id),
        "before": sorted(old_params),
        "after": sorted(new_params),
        "placeholders": [old_marks, new_marks],
        "sql": str(spec.get("sql") or ""),
    }
    if dry_run:
        return report
    templates = list_templates()
    for item in templates:
        if str(item.get("id")) != str(template_id):
            continue
        item["sql"] = str(spec.get("sql") or "")
        item["params"] = new_params
        item["fingerprint"] = str(spec.get("fingerprint") or item.get("fingerprint") or "")
        # 新挖出来的槽要有"默认推荐值"（快捷查询卡片/看板刷新靠它预填）：把重挖时的原始值
        # 补进每条样本的 values 里，缺了的话卡片上那几个槽就只能是空的
        # （用户提过："解析桩号米数 也没有默认推荐值，就是不知道要填什么"）。
        for sample in item.get("samples") or []:
            values_now = dict(sample.get("values") or {})
            for name, value in (spec.get("values") or {}).items():
                values_now.setdefault(str(name), str(value))
            sample["values"] = values_now
        spec_match = spec.get("profile") or {}
        match = item.setdefault("match", {})
        match["conditions"] = sorted({str(name) for name in (spec.get("declares") or {}).values()})
        # 重挖后必须以“本次仍挖不动的条件”为准；旧 fixed 里若已经变成参数，
        # 继续保留会把 `{task_number}` 同时声明成固定值，前端仍显示“无可换参数”。
        match["fixed"] = {
            str(k): str(v) for k, v in (spec.get("fixed_extra") or {}).items()
        }
        item["match"] = match
        item["partial"] = bool(question_is_partial(str(item.get("question") or ""), []))
        # SQL 变了：旧审核结论作废，下次巡检/审核会重审
        item.pop("review", None)
        item["review_reason"] = "槽位重挖过，审核结论已作废，请重新审核"
        log_event(
            "templatize",
            "INFO",
            "template_remined",
            template=str(template_id),
            before=len(old_params),
            after=len(new_params),
        )
    _write(templates)
    return report


def list_templates() -> list[dict[str, Any]]:
    return list(load().get("templates") or [])


def find(template_id: str) -> dict[str, Any] | None:
    for item in list_templates():
        if str(item.get("id")) == str(template_id):
            return item
    return None


def find_by_question(question: str) -> dict[str, Any] | None:
    """按"问法同一句"找它沉淀出的模板（同义召回的身份链，不做关键词匹配）。

    模板自带 `question` / `variants` / `samples[].question`——这些就是"这条模板验证过的问法"。
    示例库那边（向量召回 + 严格同问判定）已经能把"换个说法"的句子对齐到某条已验证问法，
    这里只做一次身份对齐，把召回结果落到模板上。归一化复用 nl2sql 的同一套规则，
    避免两处"同一句"的判定标准不一致。
    """
    from . import nl2sql

    key = nl2sql._example_key(str(question or ""))
    if len(key) < 4:
        return None
    for template in list_templates():
        candidates = [template.get("question"), *(template.get("variants") or [])]
        candidates += [item.get("question") for item in template.get("samples") or []]
        if any(nl2sql._example_key(str(item or "")) == key for item in candidates):
            return template
    return None


def set_enabled(template_id: str, enabled: bool) -> bool:
    templates = list_templates()
    changed = False
    for item in templates:
        if str(item.get("id")) == str(template_id):
            item["enabled"] = bool(enabled)
            changed = True
    if changed:
        _write(templates)
    return changed


def promote(template_id: str) -> bool:
    """人工把 candidate 转正成 mature（维护页的"转正"按钮）。

    为什么要人工这一档：枚举类槽（上下行/收藏/工单状态/有无图）的"写死值"可能是有意为之，
    跨样本证据不足时系统只敢记为 candidate；管理员确认"这个条件本来就该换值"后转正
    （docs/复杂模板匹配与推荐改造方案.md P2-9）。
    """
    templates = list_templates()
    changed = False
    for item in templates:
        if str(item.get("id")) != str(template_id):
            continue
        item["status"] = "mature"
        item["promoted_at"] = _now()
        changed = True
    if changed:
        _write(templates)
        log_event("templatize", "INFO", "promoted", template=template_id)
    return changed


_QUESTION_ASK = re.compile(r"(多少|几次|几条|几处|几个|几座|哪种|哪些|多大|多长|多少米|最高|最低|最多|最少|分别|各)")


def _ask_count(text: str) -> int:
    """这个问题里有几个"提问子句"（问号数 与 并列的提问子句数 取大）。"""
    marks = len(re.findall(r"[？?]", str(text or "")))
    clauses = [part for part in re.split(r"[，,；;、]", str(text or "")) if len(part.strip()) >= 4]
    asks = len([part for part in clauses if _QUESTION_ASK.search(part)])
    return max(marks, asks)


def question_is_partial(question: str, metrics: list[str] | None = None) -> bool:
    """这个问题是不是**多子问题**（沉淀出来的模板多半只覆盖其中一部分）。

    docs/模板沉淀标准与复用校验_系统性方案.md §4.3：模板若只答了一部分，必须让用户看到
    "这只回答了第 X 部分"，不能假装答完整了。

    判据：先数提问子句（问号数 / 并列的提问子句），再看**模板自己声明了几个指标**——
    "有多少处？总面积多少？"这种模板声明了 count+total_area 就是答全了，不该误标；
    "上下行各多少？最多的类型是什么？"只声明 count 一条，才是真的只答了一部分。
    metrics=None（不知道模板覆盖多少，例如只看示例问法）时按保守口径：多问句就算 partial。
    """
    text = str(question or "").strip()
    if not text:
        return False
    asks = _ask_count(text)
    if asks < 2:
        return False
    if metrics is None:
        return True
    declared = [str(item) for item in metrics if str(item or "").strip()]
    return len(declared) < asks


def _offered_counts(window: int = 800) -> dict[str, int]:
    """最近 window 条"候选收集"日志里，每条模板被摆出来的次数。

    从 agent_logs 现算，而不是每题去写注册表（写盘太频繁，也容易并发冲突）。
    日志读不到（表还没建/查询失败）就返回空——巡检宁可少判，不能误判成 cold。
    """
    counts: dict[str, int] = {}
    try:
        from ..store import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM agent_logs WHERE event = 'collected' "
                "ORDER BY id DESC LIMIT ?",
                (int(window),),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "offered_counts_failed", exc)
        return counts
    for row in rows:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except (ValueError, TypeError):
            continue
        for name in str(payload.get("ids") or "").split("、"):
            name = name.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def sweep_templates(
    cold_after: int = 0, log_window: int = 800, dry_run: bool = False
) -> dict[str, Any]:
    """生命周期巡检（docs/模板沉淀标准与复用校验_系统性方案.md §5.3）。

      · **broken**：模板 SQL 引用的表/列在当前字段目录里找不到 → 标记 broken，不进推荐；
      · **cold**：被摆出来 ≥ N 次却一次没被复用 → 标记 cold，不进推荐但保留（防误删）。

    两条都只改状态、不改 SQL（§八）。字段目录为空（读库失败）时**不判 broken**——
    目录读不到就断言"表不存在"会把好模板全标坏（这个坑在字段触达那轮踩过）。
    """
    from . import static_check

    # 顺手按**当前**规则把存量审核标记归一化（零 LLM）：规则改了、存量记录不会自己变，
    # 实测有 13 条被旧规则标成"该降级"，其中 10 条按现规则是误报（见 normalize_reviews）。
    if not dry_run:
        normalize_reviews()
    if not int(cold_after):
        cold_after = int(getattr(settings, "template_cold_after_offers", 10) or 10)
    try:
        catalog_ready = bool(schema_catalog.field_catalog("mysql").get("tables"))
    except Exception:  # noqa: BLE001
        catalog_ready = False
    offered = _offered_counts(window=log_window)
    templates = list_templates()
    broken: list[str] = []
    cold: list[str] = []
    changed = False
    for item in templates:
        if str(item.get("status")) not in {"mature", "candidate"}:
            continue
        template_id = str(item.get("id"))
        if catalog_ready:
            try:
                problems = static_check.check_sql(str(item.get("sql") or ""), "mysql")
            except Exception as exc:  # noqa: BLE001
                log_error("templatize", "sweep_check_failed", exc, template=template_id)
                problems = []
            fatal = [
                text
                for text in problems
                if str(text).startswith(
                    (
                        "表不在字段目录里",
                        "列不存在",
                        "CTE 字段契约不成立",
                        "派生表字段契约不成立",
                    )
                )
            ]
            if fatal:
                item["status"] = "broken"
                item["broken_reason"] = str(fatal[0])[:120]
                broken.append(template_id)
                changed = True
                log_event("templatize", "WARNING", "template_broken", template=template_id, reason=str(fatal[0])[:80])
                continue
        shown = int(offered.get(template_id, 0))
        used = int(item.get("usage") or 0)
        # 太新的模板不判冷门：刚挖出来就"被摆很多次没人用"多半是跑批/回归刷出来的，
        # 不是真实用户行为（实测一次回归跑批就把新模板标成了 cold）
        age_days = 0.0
        created = str(item.get("created_at") or "")
        if created:
            try:
                age_days = (time.time() - time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))) / 86400.0
            except Exception:  # noqa: BLE001
                age_days = 0.0
        min_age = float(getattr(settings, "template_cold_min_age_days", 7.0) or 0.0)
        if (
            shown >= cold_after
            and used == 0
            and str(item.get("status")) == "mature"
            and age_days >= min_age
        ):
            item["status"] = "cold"
            item["cold_reason"] = f"被摆出 {shown} 次、一次没被复用"
            cold.append(template_id)
            changed = True
            log_event("templatize", "INFO", "template_cold", template=template_id, offered=shown)
    if changed and not dry_run:
        _write(templates)
    unmined = flag_unmined_literals(dry_run=dry_run)
    return {
        "checked": len(templates),
        "broken": broken,
        "cold": cold,
        "unmined": unmined.get("items") or [],
        "catalog_ready": catalog_ready,
        "dry_run": bool(dry_run),
    }


_HIGH_SEVERITY = {"high", "严重", "高"}
# 只有这一类"high"才**可能**自动降级（实测：LLM 对"写死了过滤值"这类判断误报率高，
# 而写死条件的准入卡片那边已有确定性校验；误降级会白白丢掉好模板的召回）。
# `partial_answer` 已从这份名单里去掉：它要判"SQL 有没有答全问法里的指标"，而这在本项目里
# 是**确定性判定**（`question_is_partial`，模板上的 `partial` 标记就是它算的）——再让模型投一次票，
# 实测 6/6 误报（三处指标都答了的 SQL 被判成"只答一部分"，理由是"多子问题标记是 false"）。
_DEMOTE_ISSUE_TYPES = {"parameter_misaligned"}

# 实体槽给人看的中文名（`short_name` 这种物理列名直接当 label 显示，维护页看不懂）
_SLOT_LABELS = {"route": "路线", "damage_type": "病害类型", "section": "路段"}

# 时间粒度的**封闭词表**：格式串 ↔ 中文名。挖掘侧用它做枚举槽的选项，
# 计划侧用它把语义层的 group_by 翻成格式串（谁都不猜措辞）。
_TIME_BUCKET_FORMATS = {
    "%Y": "年",
    "%Y-%m": "月",
    "%Y-%m-%d": "日",
    "%x-%v": "周",
    "%Y-%m-%d %H": "小时",
}
_TIME_BUCKET_BY_GROUP = {
    "year": "%Y",
    "month": "%Y-%m",
    "day": "%Y-%m-%d",
    "week": "%x-%v",
    "hour": "%Y-%m-%d %H",
}
# 挖槽时读到的是 **sqlglot 归一后的格式串**（实测 `%x-%v` → `%G-%v`，ISO 年用的是 %G），
# 而生成 SQL 时要用 MySQL 认的写法 → 这里做一次别名归一，两边就不会对不上。
_TIME_BUCKET_ALIASES = {
    "%G-%v": "%x-%v",
    "%G-%u": "%x-%v",
    "%Y-%v": "%x-%v",
    "%Y-%u": "%x-%v",
}


def has_high_issues(verdict: dict[str, Any] | None) -> bool:
    """审核结论里有没有 high 级问题（只有 high 才降级，误报代价太大）。"""
    for item in (verdict or {}).get("issues") or []:
        if str((item or {}).get("severity") or "").strip().lower() in _HIGH_SEVERITY:
            return True
    return False


def has_duplicate_slots(template: dict[str, Any] | None) -> bool:
    """SQL 里有没有"同一类参数的多个槽"（`{route_id}` + `{route_id_2}`）。

    这是"参数错位"的**必要条件**：只有一个同类参数时，参数与别名/子查询根本不可能对不上。
    """
    sql = str((template or {}).get("sql") or "")
    names = re.findall(r"\{+([a-z_0-9]+)\}+", sql)
    groups: dict[str, set[str]] = {}
    for name in names:
        groups.setdefault(re.sub(r"_\d+$", "", name), set()).add(name)
    return any(len(items) >= 2 for items in groups.values())


def should_demote(
    verdict: dict[str, Any] | None, template: dict[str, Any] | None = None
) -> bool:
    """该不该按审核结论降级：**只认"参数错位 / 只答一部分"这两类 high**。

    其它 high（写死条件、别名写死、单位）只记进 `review` 供人看——实测 LLM 会把
    "模板问法里本来就写了的条件"（`problem_type = '泛油'`）和"占位符"（`{route_id}`）
    误报成 hardcoded_condition，按它降级会把好模板打下去（false positive 实测 3/3）。

    而且 `parameter_misaligned` 还要**代码级前提**：SQL 里真有两个同类占位符才可能错位。
    实测 LLM 会把"问法里写了路线名 + SQL 里有 `{route_id}`"当成错位（误报 2/2）——
    那只说明这个槽是参数化的，恰恰是正确的写法。
    """
    for item in (verdict or {}).get("issues") or []:
        if str((item or {}).get("severity") or "").strip().lower() not in _HIGH_SEVERITY:
            continue
        issue_type = str((item or {}).get("type") or "").strip().lower()
        if issue_type not in _DEMOTE_ISSUE_TYPES:
            continue
        if issue_type == "parameter_misaligned" and template is not None:
            if not has_duplicate_slots(template):
                continue
            return True
        return True
    return False


def apply_review(template_id: str, verdict: dict[str, Any] | None) -> dict[str, Any]:
    """把审核结论落到模板记录上（方案 §5.1 + 模板挖槽位与LLM审核逻辑重构方案.md 方案 A）：

      · 结论缓存进 `review`（同一模板不重复审）：`would_demote` = 审核建议，`demote` = 这次真降了没；
      · **默认不按审核结论改状态**（`TEMPLATE_REVIEW_AUTO_DEMOTE=false`）：LLM 审核误报率高，
        自动降级等于拿误报丢好模板；开关打开时也只认"参数错位"这一类（见 _DEMOTE_ISSUE_TYPES）；
      · 通过就只记结论——不靠 LLM 自动转正（转正仍由原有的回环验证 + 用过的次数决定）。
    只改状态、不改 SQL（§八）。
    """
    auto = bool(getattr(settings, "template_review_auto_demote", False))
    templates = list_templates()
    found = False
    demoted = False
    restored = False
    for item in templates:
        if str(item.get("id")) != str(template_id):
            continue
        # 代码级前提（同类占位符 ≥2）也过一遍：单槽模板的 parameter_misaligned 不算
        would = should_demote(verdict, item)
        item["review"] = {
            "pass": bool((verdict or {}).get("pass")),
            "high": has_high_issues(verdict),
            # 审核建议（不管开不开自动降级都记下来，维护页照旧展示）
            "would_demote": would,
            # 这次**真的**按审核结论降级了没有（前端"已降级"标签只认这个）
            "demote": bool(auto and would),
            "auto_demote_enabled": auto,
            "issues": list((verdict or {}).get("issues") or [])[:6],
            "suggested_fixes": list((verdict or {}).get("suggested_fixes") or [])[:4],
            "at": _now(),
        }
        if not auto:
            found = True
            continue
        if would and str(item.get("status")) == "mature":
            item["status"] = "candidate"
            item["demote_reason"] = "LLM 审核发现高风险问题：" + str(
                ((verdict or {}).get("issues") or [{}])[0].get("description") or ""
            )[:100]
            demoted = True
        elif (
            not would
            and str(item.get("status")) == "candidate"
            # 只恢复"被 LLM 审核降下去的"——人工降级/待观察的模板不自动转正
            and str(item.get("demote_reason") or "").startswith("LLM 审核发现")
        ):
            item["status"] = "mature"
            item["promoted_at"] = _now()
            item.pop("demote_reason", None)
            restored = True
        found = True
    if found:
        _write(templates)
    return {
        "applied": found,
        "demoted": demoted,
        "restored": restored,
        "high": has_high_issues(verdict),
        "would_demote": should_demote(verdict),
        "auto_demote": auto,
    }


def flag_unmined_literals(dry_run: bool = False) -> dict[str, Any]:
    """把"SQL 里还写着用户问的数字、却没挖成槽"的模板标出来（不进推荐）。

    为什么单列一道：这类模板**能跑、结果还看着对**，只是换个值就静默答错
    ——典型 `ORDER BY ABS(p.pile_distance - 1802)`（1802 是用户问的 k1+802，
    换 k1+000 复用排序基准还是 1802，还不报错）。它是"最危险的一类错"。
    新闸口（`unmined_question_numbers`）已经不让它再被挖出来；这一趟负责把**存量**翻出来。

    只做纯 AST 检查（把**参数化** SQL 里的占位符换成"哨兵值"再跑同一套判据），
    不调用 LLM、不查库。**必须查参数化 SQL**：回填成原始 SQL 再查，等于把所有用户值
    都当成"没挖成槽"（第一版就这么写错了，一次误标 9 条）。
    """
    templates = list_templates()
    fixed_cfg = set(_cfg().get("fixed_columns") or [])
    items: list[dict[str, Any]] = []
    dirty = False
    for item in templates:
        question = str(item.get("question") or "")
        checked = _sentinel_sql(item)
        try:
            tree = sqlglot.parse_one(checked, read="mysql")
        except Exception:  # noqa: BLE001
            continue
        fixed = fixed_cfg | set(((item.get("match") or {}).get("fixed") or {}).keys())
        found = unmined_question_numbers(tree, question, fixed)
        if not found:
            continue
        reason = "SQL 里还写着用户问的数字、没挖成槽：" + "、".join(
            f"{value}（{where}）" for value, where in found[:2]
        ) + "——换值复用会静默答错"
        items.append({"id": str(item.get("id")), "question": question[:40], "reason": reason})
        if not dry_run:
            if not item.get("risk"):
                item["risk"] = reason
            item["unmined_reason"] = reason
            if str(item.get("status")) == "mature":
                item["status"] = "candidate"
            dirty = True
    if dirty and not dry_run:
        _write(templates)
        log_event("templatize", "INFO", "templates_with_unmined_literals", count=len(items))
    return {"checked": len(templates), "flagged": len(items), "items": items, "dry_run": dry_run}


def _bucket_granularities(tree: Any) -> list[tuple[str, str, str]]:
    """按**形状**找出"分档粒度"字面值 → [(字面值, 所在列)]。

    为什么不靠关键词问法：粒度是在 SQL 结构里体现的，认结构比认措辞稳。认这几种形状：
      · `FLOOR(col / N)` / `col / N`（FLOOR/CEIL/ROUND 里按 N 分档）
      · `FLOOR((col % M) / N) * N`（先取模再分档：`% M` 是单位换算，`N` 才是粒度）
      · `col % N`（直接按 N 取模分桶）
    排除项：`% M` 的模数（公里→米那类换算，不是用户粒度）、数据类型/精度里的数字、
    LIMIT（走 top_n 槽，不在这条路上）。
    """
    moduli: set[float] = set()
    for node in tree.find_all(exp.Mod) if tree is not None else []:
        right = node.expression
        if isinstance(right, exp.Literal) and right.is_number:
            moduli.add(_number(right))
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for node in tree.find_all(exp.Div) if tree is not None else []:
        left, right = node.this, node.expression
        if not isinstance(right, exp.Literal) or not right.is_number:
            continue
        if left.find(exp.Column) is None:
            continue  # 分母上的数字要配上"列"才算分档
        value = _number(right)
        if value is None or value in moduli or value <= 1:
            continue  # 取模那一路是单位换算；1 这种也没意义
        column = str((left.find(exp.Column) or exp.column("")).name or "")
        key = f"{value}"
        if key in seen:
            continue
        seen.add(key)
        # 替换只在**这一段表达式**里做（全局替换会误伤 DECIMAL(10,6)、ROUND(x,2) 这类结构常量）
        scope = node
        parent = node.parent
        while parent is not None and isinstance(
            parent, (exp.Floor, exp.Ceil, exp.Round, exp.Paren, exp.Mul)
        ):
            scope = parent
            parent = parent.parent
        found.append(
            (right.sql(dialect="mysql").strip("'"), column, scope.sql(dialect="mysql"))
        )
    return found


def _number(node: Any) -> float | None:
    try:
        return float(str(node.this))
    except (TypeError, ValueError):
        return None


def _parameterize_buckets(
    text: str,
    params: dict[str, Any],
    values: dict[str, str],
    declares: dict[str, str],
) -> tuple[str, dict[str, Any], dict[str, str], dict[str, str], list[str]]:
    """把"分档粒度"字面值换成槽（确定性、不靠模型、不靠关键词）。

    实测 `FLOOR((pile_distance % 1000) / 100) * 100` 这条：`100` 是用户说的"每 100 米一段"，
    写死就等于换"每 500 米"时还是按 100 分档（**静默答错**）；而 `% 1000` 是公里→米换算，
    必须原样留着。两边的区别在结构上分得清，所以这里不用问模型。
    和语义补漏一样，替换后要能**反向还原**回替换前，否则整条放弃。
    """
    original = text
    try:
        tree = sqlglot.parse_one(text, read="mysql")
    except Exception:  # noqa: BLE001
        return original, params, values, declares, []
    applied: list[str] = []
    undo: list[tuple[str, str]] = []
    for literal, column, fragment in _bucket_granularities(tree):
        if fragment not in text:
            continue
        base = f"num_{column}_bucket" if column else "num_bucket"
        name = base
        suffix = 2
        while name in params:
            if str(values.get(name) or "") == literal:
                break  # 同一粒度已经挖过
            name = f"{base}_{suffix}"
            suffix += 1
        if name in params:
            continue
        pattern = re.compile(r"(?<![\w.])" + re.escape(literal) + r"(?![\w.])")
        if not pattern.search(fragment):
            continue
        new_fragment = pattern.sub("{" + name + "}", fragment)
        undo.append((new_fragment, fragment))
        text = text.replace(fragment, new_fragment, 1)
        params[name] = {
            "label": "分档粒度（米）",
            "column": column or "pile_distance",
            "type": "numeric",
            "required": True,
            "confirmed_by": "domain",
            "declare": "bucket",
        }
        values[name] = literal
        declares[name] = "bucket"
        applied.append(name)
    if not applied:
        return original, params, values, declares, []
    restored = text
    for replaced, previous in reversed(undo):
        restored = restored.replace(replaced, previous, 1)
    if re.sub(r"\s+", "", restored) != re.sub(r"\s+", "", original):
        return original, params, values, declares, []
    return text, params, values, declares, applied


def _sentinel_sql(template: dict[str, Any]) -> str:
    """参数化 SQL 里的占位符 → "哨兵值"（数量级远离任何用户数字，纯为解析/比对用）。

    数值槽给一个大数、比较符给 `>`、字符串类给 `'zzsentinel'`：这样剩下的**真字面值**
    才是"没挖成槽的用户值"，不会被占位符本身干扰（占位符是字符串，本来就跳过）。
    """
    text = str(template.get("sql") or "")
    for name, spec in (template.get("params") or {}).items():
        kind = str(spec.get("type") or "")
        if kind == "op":
            value = ">"
        elif kind == "numeric":
            value = "987654321.5"
        elif kind == "int":
            value = "987654321"
        else:
            value = "zzsentinel"
        quoted = "'{" + name + "}'"
        if quoted in text:
            text = text.replace(quoted, "'" + value + "'")
        else:
            text = text.replace("{" + name + "}", value)
    return text


def normalize_reviews() -> dict[str, Any]:
    """按**当前**规则重算存量模板的审核标记（零 LLM、不改 SQL）。

    为什么需要：`review.demote` / `would_demote` 和"被审核降级"的状态是**当时**的规则算的，
    规则改了存量记录不会跟着变——实测 13 条被标"该降级"的里多条是误报（只含一个 `{route_id}`
    的正常模板被判"参数错位"、三处指标都答了的 SQL 被判"只答一部分"），状态还停在 candidate。
    这里只做两件事：按现规则重算标记；把"当年被审核规则降下去的"恢复回 mature
    （人工降级 / 待观察的不动）。
    """
    auto = bool(getattr(settings, "template_review_auto_demote", False))
    templates = list_templates()
    changed = 0
    restored = 0
    for item in templates:
        review = item.get("review") or {}
        if not review:
            continue
        would = should_demote({"issues": review.get("issues") or []}, item)
        if bool(review.get("would_demote")) != would or bool(review.get("demote")) != bool(
            auto and would
        ):
            review["would_demote"] = would
            review["demote"] = bool(auto and would)
            review["auto_demote_enabled"] = auto
            item["review"] = review
            changed += 1
        if (
            not would
            and str(item.get("status")) == "candidate"
            and str(item.get("demote_reason") or "").startswith("LLM 审核发现")
        ):
            item["status"] = "mature"
            item["promoted_at"] = _now()
            item.pop("demote_reason", None)
            restored += 1
    if changed or restored:
        _write(templates)
        log_event(
            "templatize", "INFO", "reviews_normalized", changed=changed, restored=restored
        )
    return {
        "checked": len(templates),
        "changed": changed,
        "restored": restored,
        "auto_demote": auto,
    }


def review_template(template_id: str) -> dict[str, Any]:
    """用 LLM 审一条模板（qwen3-coder-plus 角色），结论落库。审核失败**不改变状态**。"""
    from . import prompts
    from .llm import LLMError, chat_json, role_model

    template = find(template_id)
    if not template:
        return {"reviewed": False, "reason": "没有这条模板"}
    match = template.get("match") or {}
    try:
            verdict = chat_json(
                prompts.messages(
                    "template_review",
                    question=str(template.get("question") or "")[:200],
                    sql=str(template.get("sql") or "")[:6000],
                    conditions="；".join(
                        [str(item) for item in (match.get("conditions") or [])]
                        + [f"{key}={value}" for key, value in (match.get("fixed") or {}).items()]
                    )
                    or "（没声明条件）",
                ),
                model=role_model("sql"),
            )
    except (LLMError, Exception) as exc:  # noqa: BLE001
        log_error("templatize", "template_review_failed", exc, template=template_id)
        return {"reviewed": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}
    result = apply_review(template_id, verdict if isinstance(verdict, dict) else {})
    # 审核意见回喂挖槽（用户定的链路：审核发现问题 → 交给语义理解去挖，而不是记一条建议就完了）。
    # 只送 parameter_misaligned：它说的就是"这处该参数化却没参数化"，正是挖槽能修的东西。
    hints = [
        str(item.get("location") or item.get("description") or "")
        for item in ((verdict or {}).get("issues") or [])
        if isinstance(item, dict)
        and str(item.get("type") or "").strip().lower() == "parameter_misaligned"
        and str(item.get("location") or item.get("description") or "").strip()
    ]
    remined: dict[str, Any] = {}
    if hints:
        try:
            remined = remine_sql(template_id, dry_run=False, hints=hints)
        except Exception as exc:  # noqa: BLE001
            log_error("templatize", "review_remine_failed", exc, template=template_id)
    if remined.get("ok"):
        # 挖好了：这条意见就算处理掉了（免得用户还得手动点忽略）
        result["remined"] = remined
    log_event(
        "templatize",
        "INFO",
        "template_reviewed",
        template=template_id,
        passed=bool((verdict or {}).get("pass")),
        high=result.get("high"),
        demoted=result.get("demoted"),
        remined=bool(remined.get("ok")),
    )
    return {"reviewed": True, "verdict": verdict, "remined": remined, **result}


def review_pending(limit: int = 3) -> dict[str, Any]:
    """给还没审过的模板补审（限量，控制花费）。挖掘/巡检在后台线程里调用。"""
    pending = [
        str(item.get("id"))
        for item in list_templates()
        if not item.get("review") and str(item.get("status")) in {"mature", "candidate"}
    ]
    reviewed: list[dict[str, Any]] = []
    for template_id in pending[: max(int(limit), 0)]:
        reviewed.append({"id": template_id, **review_template(template_id)})
    return {"pending": len(pending), "reviewed": len(reviewed), "items": reviewed}


def ignore_review(template_id: str) -> bool:
    """把审核建议标成"已忽略"（用户判定为误报）：结论留着备查，界面不再提示、也不降级。

    误报是 LLM 审核的常态（见 _DEMOTE_ISSUE_TYPES 的注释），所以除了"不自动降级"之外，
    还要给用户一个"这条我看过了、是误报"的动作，免得同一条建议反复占用注意力。
    """
    templates = list_templates()
    found = False
    for item in templates:
        if str(item.get("id")) != str(template_id):
            continue
        review = dict(item.get("review") or {})
        if not review:
            return False
        review["ignored"] = True
        review["ignored_at"] = _now()
        review["demote"] = False  # 误报不降级：真降过的由 normalize_reviews 恢复
        item["review"] = review
        found = True
    if found:
        _write(templates)
        log_event("templatize", "INFO", "review_ignored", template=str(template_id))
    return found


def demote(template_id: str, reason: str = "") -> bool:
    """把 mature 模板降级回 candidate（连续答错时自动调用）。

    见 docs/模板沉淀标准与复用校验_系统性方案.md §5.2：同一模板连续 3 次被点"答错了"
    → 自动降级、退出推荐，等人工/LLM 复核。只降级、不改 SQL（§八：不自动修复）。
    """
    templates = list_templates()
    changed = False
    for item in templates:
        if str(item.get("id")) != str(template_id):
            continue
        if str(item.get("status")) == "candidate":
            return False
        item["status"] = "candidate"
        item["demoted_at"] = _now()
        item["demote_reason"] = str(reason or "连续答错")
        changed = True
    if changed:
        _write(templates)
        log_event("templatize", "WARNING", "demoted", template=template_id, reason=reason[:80])
    return changed


def delete(template_id: str) -> bool:
    templates = list_templates()
    kept = [item for item in templates if str(item.get("id")) != str(template_id)]
    if len(kept) == len(templates):
        return False
    _write(kept)
    return True


def source_docs(template_id: str) -> list[str]:
    """这条模板是**从哪些示例问答挖出来的**（"已挖掘台账"里 template=本模板 的 doc 名）。

    模板记录本身只存 question/variants，没有回溯源头的字段；台账正好是 doc → template 的
    反向索引（一条模板可能合并了多条同结构问答，所以返回列表）。
    """
    wanted = str(template_id or "")
    if not wanted:
        return []
    mined = load().get("mined") or {}
    return [
        str(name)
        for name, entry in mined.items()
        if str((entry or {}).get("template") or "") == wanted
    ]


def forget_sources(doc_names: list[str]) -> int:
    """删掉台账里这些示例的条目。

    示例都删了还留着条目有两个坏处：台账里挂着一个**已经不存在的模板 id**（维护页看着莫名），
    而且增量挖掘按 doc 名跳过——以后同名的（修好的）文档再同步进来也永远挖不到。
    """
    names = {str(name) for name in doc_names if name}
    if not names:
        return 0
    data = load(force=True)
    mined = dict(data.get("mined") or {})
    removed = 0
    for name in names:
        if mined.pop(name, None) is not None:
            removed += 1
    if removed:
        _write(list(data.get("templates") or []), mined=mined)
    return removed


def drop_multi_object_templates() -> dict[str, Any]:
    """清掉"多对象对比"沉淀出来的自动模板（方案 §2.2-6：这类不许沉淀）。

    判据用模板自己的问法（≥2 条路线/桥隧坡/病害类型/月份）——存量模板是历史挖出来的，
    挖掘闸只拦新的，这一批要清掉；源示例保留，"相似问法召回"这条路照旧。
    """
    from . import example_reuse

    templates = list_templates()
    doomed = [
        item
        for item in templates
        if example_reuse.is_multi_object({}, str(item.get("question") or ""), deep=True)
    ]
    if not doomed:
        return {"removed": 0, "ids": []}
    doomed_ids = {str(item.get("id")) for item in doomed}
    kept = [item for item in templates if str(item.get("id")) not in doomed_ids]
    _write(kept)
    # 台账里指向这些模板的条目改成"不沉淀"的原因，下次增量挖掘不会又把它挖回来
    data = load(force=True)
    mined = dict(data.get("mined") or {})
    changed = 0
    for doc_name, entry in list(mined.items()):
        if str((entry or {}).get("template") or "") not in doomed_ids:
            continue
        mined[doc_name] = {
            "template": "",
            "reason": "多对象对比不沉淀模板（收益低风险高）",
            "at": _now(),
        }
        changed += 1
    if changed:
        _write(list(data.get("templates") or []), mined=mined)
    log_event(
        "templatize",
        "INFO",
        "multi_object_templates_dropped",
        removed=len(doomed),
        ids="、".join(sorted(doomed_ids))[:120],
    )
    return {
        "removed": len(doomed),
        "ids": sorted(doomed_ids),
        "questions": [str(item.get("question") or "")[:40] for item in doomed],
        "ledger_updated": changed,
    }


def purge_sources(doc_names: list[str]) -> dict[str, Any]:
    """删掉这些示例问答：RAGFlow 问答集 + 问题索引 + 本地示例表 + 历史记录。

    这就是"删固定看板上的卡片"要的那一步（不动模板）；模板删除的级联也复用它。
    台账条目只在**本地记录确实没了**之后才清：远端/本地都没删成时保留台账，
    否则下次增量挖掘会把这个"用户明确不想要"的问答又挖回来。
    """
    names = [str(name) for name in doc_names if name]
    if not names:
        return {
            "deleted_docs": 0,
            "deleted_index": 0,
            "removed_records": 0,
            "removed_history": 0,
            "forgotten": 0,
        }
    from . import nl2sql

    result = nl2sql.purge_example_pairs(names)
    try:
        from .. import store

        remaining = {
            str(row.get("doc_name"))
            for row in store.list_recorded_example_pairs(limit=5000)
        }
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "source_survivors_failed", exc)
        remaining = set(names)
    result["forgotten"] = forget_sources([name for name in names if name not in remaining])
    return result


def delete_with_sources(template_id: str) -> dict[str, Any]:
    """删模板 + 源头示例（用户要求的一键清理：以前只删模板，源头还留在 RAGFlow 里，
    下次重挖或点"全量重挖"它又长回来）。"""
    sources = source_docs(template_id)
    if not delete(template_id):
        return {"deleted": False, "reason": f"没有这条自动模板：{template_id}"}
    result: dict[str, Any] = {"deleted": True, "id": template_id, "source_docs": sources}
    if not sources:
        return result
    try:
        result["purge"] = purge_sources(sources)
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "purge_sources_failed", exc, template=template_id)
        result["purge_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return result


def bump_usage(template_id: str) -> None:
    """记一次命中次数（维护页展示"越用越省"，失败不影响查询）。"""
    with _LOCK:
        templates = list_templates()
        for item in templates:
            if str(item.get("id")) == str(template_id):
                item["usage"] = int(item.get("usage") or 0) + 1
                try:
                    _write(templates)
                except Exception as exc:  # noqa: BLE001
                    log_error("templatize", "usage_write_failed", exc, template=template_id)
                return


# ── 目录索引：把"列名"翻成"这是哪种槽"（事实源是 schema_catalog）──────────────
def _column_index() -> dict[str, list[tuple[str, dict[str, Any]]]]:
    index: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for table in schema_catalog.field_catalog("mysql").get("tables") or []:
        name = str(table.get("table"))
        for col in table.get("columns") or []:
            index.setdefault(str(col.get("name") or "").lower(), []).append((name, col))
    return index


def _fk_display_targets() -> dict[tuple[str, str], dict[str, str]]:
    """`(表, 列)` → 它是某个外键的**显示列**时，该外键的取值列/显示列/目标表。"""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for table in schema_catalog.field_catalog("mysql").get("tables") or []:
        for col in table.get("columns") or []:
            join = col.get("join")
            if not isinstance(join, dict) or not join.get("table"):
                continue
            key = (str(join["table"]), str(join.get("column") or ""))
            out.setdefault(
                key,
                {
                    "table": str(join["table"]),
                    "value_column": str(join.get("on") or ""),
                    "label_column": str(join.get("column") or ""),
                },
            )
    return out


def _fk_target_columns() -> dict[tuple[str, str], dict[str, str]]:
    """`(表, 列)` → 它是某个外键的**目标列**（一般就是实体主键）时的实体信息。

    `r.id IN ('路线id1','路线id2')` 这种写法比的是实体主键，也是"实体引用"，
    但 `_fk_display_targets` 只登记了显示列（`short_name`）→ 这类谓词会被判成
    "不可泛化"，多路线题永远挖不成模板（docs/人工示例池全量参与模板挖掘方案.md P0-6）。
    """
    out: dict[tuple[str, str], dict[str, str]] = {}
    for table in schema_catalog.field_catalog("mysql").get("tables") or []:
        for col in table.get("columns") or []:
            join = col.get("join")
            if not isinstance(join, dict) or not join.get("table"):
                continue
            key = (str(join["table"]), str(join.get("on") or ""))
            out.setdefault(
                key,
                {
                    "table": str(join["table"]),
                    "value_column": str(join.get("on") or ""),
                    "display_column": str(join.get("column") or ""),
                },
            )
    return out


_OPTIONS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_OPTIONS_TTL = 300.0


@lru_cache(maxsize=32)
def _has_del_flag(table: str) -> bool:
    """这张表有没有 del_flag（有就按 del_flag=0 过滤字典取值）。"""
    entry = schema_catalog.table_entry("mysql", table) or {}
    return any(str(col.get("name")) == "del_flag" for col in entry.get("columns") or [])


def _entity_options(table: str, value_column: str, label_column: str) -> list[dict[str, str]]:
    """连库现查去重取值（entity/enum 槽的下拉框），不写死。"""
    for name in (table, value_column, label_column):
        if not _IDENT.match(str(name or "")):
            return []
    # 配了现成取值域的（路线→routes_with_data）就先用它：整表前 N 条会把在用的对象挤出去
    preset = (_cfg().get("entity_sources") or {}).get(table) or {}
    if preset.get("source") and str(preset.get("value_column") or value_column) == value_column:
        options = preset_lib.options_for(str(table), {"source": str(preset["source"])})
        if options:
            # preset 的下拉标签带统计后缀（"宝安大道西乡段（911 处）"），绑定要的是纯名字
            return [
                {"label": str(item.get("label") or "").split("（")[0].strip(), "value": str(item.get("value"))}
                for item in options
            ]
    key = f"{table}.{value_column}.{label_column}"
    cached = _OPTIONS_CACHE.get(key)
    if cached and time.time() - cached[0] < _OPTIONS_TTL:
        return cached[1]
    if value_column == label_column:
        sql = (
            f"SELECT DISTINCT `{value_column}` AS value FROM `{table}` "
            f"WHERE `{value_column}` IS NOT NULL AND `{value_column}` <> '' "
            f"ORDER BY value LIMIT 300"
        )
    else:
        # 字典表里有软删除的测试行（实测病害类型 97 条里夹着脏值），有 del_flag 就按它过滤
        alive = "AND `del_flag` = 0 " if _has_del_flag(table) else ""
        sql = (
            f"SELECT DISTINCT `{value_column}` AS value, `{label_column}` AS label FROM `{table}` "
            f"WHERE `{value_column}` IS NOT NULL AND `{value_column}` <> '' "
            f"{alive}"
            f"ORDER BY label LIMIT 300"
        )
    try:
        rows = db_manager.query("mysql", sql)["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "entity_options_failed", exc, table=table, column=value_column)
        return []
    options = [
        {
            "label": str(row.get("label") or row.get("value")),
            "value": str(row.get("value")),
        }
        for row in rows
        if str(row.get("value") or "")
    ]
    # 去重去脏：字典里同一类型名对应多个 code（一类多码），还夹着测试脏值
    # （实测病害类型 97 条里有重名和 adsad213223/eqweqw2313/848626 这类纯字母数字）
    options = preset_lib.clean_options(options)
    _OPTIONS_CACHE[key] = (time.time(), options)
    return options


def _classify_column(
    qualifier: str, column: str, ctx: dict[str, Any]
) -> dict[str, Any] | None:
    """按列的角色判断槽位类型；判断不出来返回 None（交 LLM 兜底或直接放弃）。"""
    table = ctx["aliases"].get(qualifier) if qualifier else None
    # JOIN 等值把两张表的列绑成同一个语义槽（`p.routes_id = r.id`）：
    # 过滤条件写在 `r.id` 上时，按等价的 `p.routes_id` 归类
    for member in ctx["equivalents"].get((str(table), str(column))) or []:
        if (str(table), str(column)) == member:
            continue
        found = _classify_plain(str(member[0]), str(member[1]), ctx)
        if found and found.get("type") != "fixed":
            return found
    return _classify_plain(str(table or ""), str(column), ctx)


def _classify_plain(table: str, column: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    col: dict[str, Any] | None = None
    requested = str(column or "")
    physical_name = ""
    if table:
        col = schema_catalog.resolve_column("mysql", table, column)
    if col is None and table:
        # MDL 语义层的**逻辑列名**（`disease.problem_type_name`、`route_name`）不是物理列：
        # 按应用里同一份字段族声明翻成物理列再归类，否则 CTE 形示例里的
        # "problem_type_name = '泛油'" 会被判"不可泛化"，整条 SQL 挖不成模板
        # （docs/推荐模板链路Bug清单.md P0-4 / P1-9）。
        from . import nl2sql as _nl2sql

        for candidate in _nl2sql._FIELD_FAMILIES.get(requested) or ():
            found = schema_catalog.resolve_column("mysql", table, str(candidate))
            if found:
                col = found
                physical_name = str(found.get("name") or "")
                break
    if col is None:
        # 派生表别名（如字典子查询的 b）→ 按列名找归属，优先"出现在本条 SQL 里的表"
        candidates = ctx["index"].get(str(column).lower()) or []
        physical = [item for item in candidates if item[0] in ctx["tables"]]
        if len(physical) == 1:
            table, col = physical[0]
        elif len(candidates) == 1:
            table, col = candidates[0]
        else:
            return None
    roles = set(col.get("roles") or [])
    name = str(col.get("name"))
    if name in set(_cfg().get("fixed_columns") or []):
        return {"type": "fixed", "table": table, "column": name}
    if name in set(_cfg().get("free_text_columns") or []):
        return None
    if "image" in roles:
        return {"type": "bool_image", "table": table, "column": name, "label": col.get("label")}
    join = col.get("join")
    if isinstance(join, dict) and join.get("table") and join.get("column"):
        # 外键列：库里存的是 code，展示用目标表的显示列
        role = _role_for_table(str(join["table"]), name)
        return {
            "type": "entity",
            "table": table,
            "column": name,
            # 槽位名给人看：路线这一槽统一叫"路线"（物理列名 `routes_id`/`route_id` 直接显示
            # 维护页看不出是什么）
            "label": _SLOT_LABELS.get(role) or col.get("label") or name,
            "role": role,
            # 库里存的是 code（`p.damage_type = '1101'`）→ 绑定时写 code；
            # 但 MDL 逻辑列名（`problem_type_name`）比的是**中文名** → 绑定时要写名字
            "form": "label" if _is_display_name(requested, physical_name) else "value",
            "options": _entity_options(
                str(join["table"]), str(join.get("on") or ""), str(join["column"])
            ),
            "multi": True,
        }
    display = _fk_display_targets().get((str(table), name))
    if display:
        target_table = str(display.get("table") or "")
        label_column = str(display.get("label_column") or "")
        # 槽位名给人看：以前直接把物理列名（`short_name`）当 label，维护页/卡片上显示成
        # "short_name·domain"，看不出这是什么；按角色取中文名，认不出来再退回目标列的中文注释
        target_col = (
            schema_catalog.resolve_column("mysql", target_table, label_column)
            if label_column
            else None
        )
        role = _role_for_table(target_table, name)
        return {
            "type": "entity",
            "table": table,
            "column": name,
            "label": (
                _SLOT_LABELS.get(role)
                or str((target_col or {}).get("label") or "")
                or col.get("label")
                or name
            ),
            "role": role,
            # 路线槽统一用 **id**：SQL 里比的是显示列（`r.short_name = '宝安大道西乡段'`）时，
            # 挖槽阶段把这一列换成 `r.id`、值换成解析出来的路线 id —— 和其他模板（都走 routes_id/id）
            # 一致，也避免"名称列被当成 id 用"这类错绑
            # （docs/确认智能体…§三 P0-1：路线槽统一 id 列）。
            "form": "label",
            "normalize_to_id": (
                str(display.get("value_column"))
                if str(display.get("table")) == "basic_facilities_routes"
                else ""
            ),
            "options": _entity_options(display["table"], display["value_column"], display["label_column"]),
            "multi": True,
        }
    target_ref = _fk_target_columns().get((str(table), name))
    if target_ref:
        # SQL 里比的就是实体主键本身（`r.id IN (…)`）→ 绑定时直接写 id
        role = _role_for_table(str(table), name)
        return {
            "type": "entity",
            "table": table,
            "column": name,
            "label": _SLOT_LABELS.get(role) or col.get("label") or name,
            "role": role,
            "form": "value",
            "options": _entity_options(
                str(table), name, str(target_ref.get("display_column") or "")
            ),
            "multi": True,
        }
    code_map = col.get("code_map") or {}
    if code_map:
        return {
            "type": "enum",
            "table": table,
            "column": name,
            "label": col.get("label") or name,
            "options": [{"label": str(label), "value": str(code)} for code, label in code_map.items()],
        }
    if "time" in roles:
        return {"type": "time", "table": table, "column": name, "label": col.get("label") or name}
    if "numeric" in roles:
        return {"type": "numeric", "table": table, "column": name, "label": col.get("label") or name}
    # 编号类文本不是枚举：它的取值集合就是业务数据本身（任务号/巡查号/病害号…）。
    # 这类列仍然可以在模板里挖成文本槽，不能因为目录没给 code_map 就把整个条件判死。
    if name.endswith("_number") and str(col.get("type") or "").lower().startswith("varchar"):
        return {
            "type": "text_slot",
            "table": table,
            "column": name,
            "label": col.get("label") or name,
        }
    return None


def _classify_expression(sql_text: str) -> dict[str, Any] | None:
    """识别"函数/JSON 包裹的裸列"（P1-4）：

    `CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position,'$.confidence')) AS DECIMAL(10,6))`
    这类表达式在 nl2sql 里已经登记成逻辑字段 confidence，这里按规范化文本比对认领，
    认下来就照常开数值槽（阈值可换），不再因为"左值不是裸列"把整条 SQL 丢掉。
    """
    from . import nl2sql

    def norm(text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()

    target = norm(sql_text)
    if not target:
        return None
    for name, triple in getattr(nl2sql, "_SPECIAL_FIELDS", {}).items():
        expr, numeric, label = triple
        if norm(expr) != target:
            continue
        return {
            "type": "numeric" if numeric else "enum",
            "table": None,
            "column": name,
            "label": label,
            "role": name,
        }
    return None


def _is_display_name(requested: str, physical_name: str) -> bool:
    """SQL 里比的是不是"实体的显示名"（`problem_type_name` / `short_name`）。"""
    name = str(requested or "")
    if not name:
        return False
    if name in {"short_name", "full_name", "problem_type", "type_name"}:
        return True
    return name.endswith("_name") and name != str(physical_name or "")


def _role_for_table(table: str, column: str) -> str:
    """实体槽对应计划里的哪种条件（路线 / 病害类型 / 其他实体列）。"""
    if table == "basic_facilities_routes":
        return "route"
    if table == "ai_problem_base":
        return "damage_type"
    if table == "basic_facilities_sections":
        return "section"
    return column


def _join_equivalents(tree: Any, aliases: dict[str, str]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """把 JOIN ON 的等值列连成等价类：`p.routes_id = r.id` → 两者是同一个语义槽。"""
    pairs: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        for node in on.find_all(exp.EQ):
            left, right = node.this, node.expression
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            lkey = (str(aliases.get(str(left.table or "")) or left.table or ""), str(left.name))
            rkey = (str(aliases.get(str(right.table or "")) or right.table or ""), str(right.name))
            pairs.append((lkey, rkey))
    classes: list[set[tuple[str, str]]] = []
    for left, right in pairs:
        hit = [item for item in classes if left in item or right in item]
        if hit:
            merged = set()
            for item in hit:
                merged |= item
                classes.remove(item)
            merged |= {left, right}
            classes.append(merged)
        else:
            classes.append({left, right})
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for group in classes:
        members = sorted(group)
        for member in members:
            out[member] = members
    return out


# ── AST 挖槽 ────────────────────────────────────────────────────────────────
def _and_children(node: Any) -> list[Any] | None:
    """顶层 AND 拆分；出现 OR/XOR 就返回 None（带括号的复杂逻辑不挖，宁可不模板化）。"""
    if isinstance(node, exp.And):
        left = _and_children(node.this)
        right = _and_children(node.expression)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, (exp.Or, exp.Xor)):
        return None
    return [node]


def _literal_value(node: Any) -> tuple[str, bool] | None:
    """取字面值：返回 (值文本, 是否字符串)。NULL / 列 / 表达式一律返回 None。"""
    """取字面值：返回 (值文本, 是否字符串)。NULL / 列 / 表达式一律返回 None。"""
    if isinstance(node, exp.Literal):
        return str(node.this), bool(node.is_string)
    return None


def _parameterize_aliases(tree: Any, values: dict[str, str]) -> None:
    """投影别名里写死的阈值也换成占位符：`AS 面积大于10平方米病害数` → `AS 面积大于{num_area}平方米病害数`。

    绑定时整条 SQL 一起替换（`bind` 就是文本替换），所以卡片表头会跟着用户给的阈值走，
    不会出现"表头写着大于 10、实际按 5 查完还叫 10"这种自相矛盾的结果。
    只在**整个数字**（前后不是数字或小数点）出现在别名里时替换，避免把"10月"这种偶合改掉。
    """
    targets = {str(value): name for name, value in (values or {}).items() if str(value)}
    if not targets:
        return
    for item in tree.expressions or []:
        alias = str(item.alias or "")
        if not alias:
            continue
        for value, name in targets.items():
            pattern = re.compile(r"(?<![\d.])" + re.escape(value) + r"(?![\d.])")
            if not pattern.search(alias):
                continue
            item.set(
                "alias",
                exp.TableAlias(
                    # quoted=False：别名原样渲染（`AS 面积大于10平方米病害数`），不要因为里面有
                    # `{num_area}` 就被加上反引号——那会让回环验证判"结构不一致"（签名是按渲染文本比的）
                    this=exp.to_identifier(
                        pattern.sub("{" + name + "}", alias), quoted=False
                    )
                ),
            )
            break


def _ctx_for(tree: Any) -> dict[str, Any]:
    aliases: dict[str, str] = {}
    tables: set[str] = set()
    cte_sources = _cte_source_tables(tree)
    for table in tree.find_all(exp.Table):
        name = str(table.name or "")
        if not name:
            continue
        # CTE 引用要换回它底下的物理表：Wren 物理 SQL 的别名（p/r）指向的是 CTE 名，
        # 不换回去列归类（`p.del_flag` 属于哪张表）就会失败，CTE 形示例一律挖不成模板（P0-4）。
        resolved = cte_sources.get(name, name)
        tables.add(resolved)
        aliases[str(table.alias or name)] = resolved
        aliases.setdefault(name, resolved)
    return {
        "aliases": aliases,
        "tables": tables,
        "index": _column_index(),
        "equivalents": _join_equivalents(tree, aliases),
        "seq": 0,
        # 已经用掉的参数名：同一列出现两个阈值（`area > 10 AND area < 100`、
        # 分档 CASE 里的两个 `area > x`）时必须各占一个槽，否则两个占位符同名，
        # 绑定时会被同一个值覆盖——SQL 静默变错（实测 `area > 10 AND area < 100`
        # 挖出来两条一模一样的 `{num_area_op} {num_area}`）。
        "taken": set(),
        # (列, 比较符, 值) → 槽名：同一个阈值在 SQL 里出现多次（`SUM(CASE WHEN area > 10 …)` 与
        # 占比里再写一遍）是**同一个条件**，必须共用一个槽，否则卡上会摆两个一模一样的参数、
        # 用户改了其中一个两边就对不上了。
        "by_predicate": {},
    }


_TABLE_ROWS_CACHE: dict[str, tuple[float, int]] = {}


def table_rows(table: str, kind: str = "mysql") -> int:
    """这张表现在有多少行（带 TTL 缓存；只为"空表不能算 mature"这条判断服务）。

    只读：`SELECT COUNT(*)`，不写任何东西。查不通返回 -1（当作"不知道"，不因此降级）。
    """
    key = f"{kind}.{table}"
    now = time.time()
    cached = _TABLE_ROWS_CACHE.get(key)
    if cached and now - cached[0] < float(settings.reference_data_ttl_seconds):
        return cached[1]
    count = -1
    try:
        rows = db_manager.query(kind, f"SELECT COUNT(*) AS n FROM {table} LIMIT 1")["rows"]
        count = int(rows[0]["n"]) if rows else -1
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "table_rows_failed", exc, table=table)
    _TABLE_ROWS_CACHE[key] = (now, count)
    return count


def empty_source_tables(sql: str, kind: str = "mysql") -> list[str]:
    """这条 SQL 依赖的**空表**（0 行）；查不通的表不算（返回 -1）。

    为什么需要：空表上"跑一遍看结果"验不出口径错——回环验证比的是行数/内容，
    0 行 == 0 行永远成立（docs/从打地鼠到回归防护体系方案.md §六.③）。
    """
    try:
        tree = sqlglot.parse_one(str(sql or ""), read="mysql")
    except Exception:  # noqa: BLE001
        return []
    known = {
        str(item.get("table"))
        for item in schema_catalog.field_catalog(kind).get("tables") or []
    }
    sources = set(_cte_source_tables(tree).values())
    for table in tree.find_all(exp.Table):
        name = str(table.name or "")
        if name in known:
            sources.add(name)
    return sorted({name for name in sources if name in known and table_rows(name, kind) == 0})


def _fixed_condition_of(predicate_sql: str) -> tuple[str, str]:
    """把"挖不动的谓词"翻成一个**可声明的固定条件** (key, value)；认不出来返回 ("", "")。

    这类谓词会原样留在 SQL 里（口径固定），所以必须在 `match.fixed` 里声明出来：
    不声明的话，模板会去接**不含这个条件**的问题，等于偷偷加上过滤条件改口径
    （实测「…的 377 处"未识别"病害按桩号分段」里 `COALESCE(problem_type_name,'未识别')='未识别'`
    被跳过又没声明 → 换一句"按桩号分段统计"也会接上，结果只统计未识别病害）。
    """
    text = str(predicate_sql or "")
    low = text.lower()
    if (
        "未识别" in text
        or "未知类型" in text
        or re.search(r"(problem_type_name|damage_type)\s+is\s+null", low)
    ):
        return "unknown_type", "true"
    return "", ""


def _unique_slot(base: str, taken: set[str]) -> str:
    """给槽位取一个没被占用的名字（`num_area` → `num_area_2` → …）。"""
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


# 长对话里"补全后的问题"带回上一轮答案的计数（"…的 377 处'未识别'病害…"）：
# 这个数字是**上下文留下的痕迹**，不是用户要过滤的条件。存成模板问法会误导
# （界面/卡片上写着 377），也会让这条模板只认得那一句话。
# 判据：数字+量词**没有出现在模板 SQL 里**（出现的就是真条件，比如"每 100 米一段"）。
_COUNT_PHRASE = re.compile(
    r"(?P<lead>这|那|该|上述|刚才|其中)?\s*(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>处|条|个|次|张|座|段|根|块|米|平方米|公里|千米|公里数|%)"
)
# 这些词后面跟着的数字**是真条件**，一律不动：阈值（大于 5）、分段粒度（每 100 米一段）、
# 区间（3 到 5 米）。判据不看 SQL 也能站得住——比"数字在不在 SQL 里"更稳。
_KEEP_LEADS = re.compile(
    r"(大于|小于|超过|低于|高于|不低于|不超过|不少于|不多于|等于|每|前|后|至|到|>=|<=|>|<)\s*$"
)


def clean_template_question(
    question: str, sql: str = "", values: dict[str, Any] | None = None
) -> str:
    """把"长对话补全后的问题"清成**可复用的问题问法**（给模板记录用，不改用户原话）。

    只做一件事：删掉**没被任何条件用到的数量短语**（上一轮答案留下的"377 处"这类）。
    判据两条，任一条成立就保留：
      · 前面是阈值/分段词（大于/超过/低于/等于/每…）——真条件；
      · 这个数字出现在 SQL 文本或参数值里（`values`）——真条件（挖出来的 SQL 里数字已换成占位符，
        必须靠 values 才能看到）。
    其余（"…的 377 处'未识别'病害…"）删掉，指代词（这/那/该/上述）跟着一起删。
    """
    text = " ".join(str(question or "").split())
    if not text:
        return text
    used = set(re.findall(r"\d+(?:\.\d+)?", str(sql or "")))
    for value in (values or {}).values():
        used |= set(re.findall(r"\d+(?:\.\d+)?", str(value)))

    def _drop(match: re.Match[str]) -> str:
        if match.group("num") in used or _KEEP_LEADS.search(text[: match.start()]):
            return match.group(0)
        # 带指代词的（"这 377 处"）整段删掉，否则会剩一个悬空的"这"
        return ""

    cleaned = _COUNT_PHRASE.sub(_drop, text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text


def _cte_source_tables(tree: Any) -> dict[str, str]:
    """CTE 名 → 它底下正对着的物理表名（只认 CTE 体里第一张真实表）。"""
    cte_names = {str(cte.alias_or_name or "").lower() for cte in tree.find_all(exp.CTE)}
    out: dict[str, str] = {}
    for cte in tree.find_all(exp.CTE):
        name = str(cte.alias_or_name or "")
        if not name:
            continue
        for table in cte.this.find_all(exp.Table):
            candidate = str(table.name or "")
            if candidate and candidate.lower() not in cte_names:
                out[name] = candidate
                break
    return out


def _slot_spec(info: dict[str, Any], column: str, extra: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "label": info.get("label") or column,
        "column": column,
        "table": info.get("table"),
        "required": True,
    }
    if info.get("form"):
        # 这个槽在 SQL 里是"存 code 的列"还是"显示列"，决定绑定时写 code 还是写中文名
        spec["form"] = info["form"]
    spec.update(extra)
    return spec


def _mine_predicate(node: Any, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """把一个谓词挖成槽。

    返回 None 表示"这个谓词挖不了"（整条 SQL 放弃模板化）；返回 {"fixed": True}
    表示这是固定业务常量（del_flag 之类），原样保留。
    可挖时返回：before/after（文本替换对）、params、values（原始值，回填验证用）、declares。
    """
    if isinstance(node, (exp.Like, exp.ILike, exp.Not, exp.RegexpLike)) or node.find(exp.Subquery):
        # LIKE（自由文本）、OR 相关逻辑、子查询：属于"绝不能泛化"的一类 → 整条 SQL 放弃
        return None
    lhs = node.this if hasattr(node, "this") else None
    # 数字写在左边（`10 < p.area`，LLM 生成的 SQL 里会有）：等价于 `p.area > 10`，
    # 换过来再走同一套；不换的话这种谓词会被判"不可泛化"、把整条 SQL 丢掉
    # ——可换的阈值不该因为写法丢掉（能动的数字都该能挖）。
    if isinstance(lhs, exp.Literal) and isinstance(
        getattr(node, "expression", None), exp.Column
    ):
        flip = {
            exp.GT: exp.LT,
            exp.LT: exp.GT,
            exp.GTE: exp.LTE,
            exp.LTE: exp.GTE,
            exp.EQ: exp.EQ,
            exp.NEQ: exp.NEQ,
        }
        swapped = flip.get(type(node))
        if swapped is None:
            return None
        node = swapped(this=node.expression.copy(), expression=node.this.copy())
        lhs = node.this
    if isinstance(lhs, exp.Column):
        column = str(lhs.name)
        qualifier = str(lhs.table or "")
        info = _classify_column(qualifier, column, ctx)
    else:
        # 不是裸列：先看是不是登记过的函数/JSON 表达式（confidence 这种），
        # 认不出来就"整段保留为固定条件"，而不是让一个谓词把整条 SQL 判死（P1-4）。
        info = _classify_expression(lhs.sql(dialect="mysql"))
        column = str((info or {}).get("column") or "")
        if info is None:
            if isinstance(lhs, (exp.Cast, exp.Anonymous, exp.Func)):
                return {"skip": True}
            return None
    if info is None:
        if isinstance(lhs, exp.Column):
            # 这一列压根不在字段目录里（窗口函数别名 `t.rnk`、派生列…）：**结构性常量**，
            # 原样保留，不让一个排名序号把整条成功 SQL 判死（P1-9）。
            # 只认"数字等值"这一种形态：字符串等值/LIKE 更像业务过滤，挖不动就不挖。
            table = str(ctx["aliases"].get(str(lhs.table or "")) or lhs.table or "")
            known = bool(table) and bool(schema_catalog.resolve_column("mysql", table, column))
            literal = _literal_value(node.expression if hasattr(node, "expression") else None)
            if (
                not known
                and isinstance(node, exp.EQ)
                and literal
                and not literal[1]
                and _NUMBER.match(literal[0])
            ):
                return {"skip": True}
        return None
    if info.get("type") == "fixed":
        return {"fixed": True}
    # 参数名/槽位声明用**等价类的规范列**（`r.id` 与 `p.routes_id` 是同一个路线槽），
    # 但替换回 SQL 的文本仍用原样写法的列
    column = str(info.get("column") or column)
    lhs_sql = lhs.sql(dialect="mysql")
    ctx["seq"] += 1
    token = _TOKEN.format(f"{ctx['seq']}x{column}")
    op = ""
    if isinstance(node, exp.EQ):
        op = "="
    elif isinstance(node, exp.NEQ):
        op = "<>"
    elif isinstance(node, exp.GT):
        op = ">"
    elif isinstance(node, exp.GTE):
        op = ">="
    elif isinstance(node, exp.LT):
        op = "<"
    elif isinstance(node, exp.LTE):
        op = "<="
    is_in = isinstance(node, exp.In)
    is_between = isinstance(node, exp.Between)
    if not op and not is_in and not is_between:
        return None
    kind = str(info.get("type"))
    if is_between:
        low = _literal_value(node.args.get("low"))
        high = _literal_value(node.args.get("high"))
        if not low or not high or low[1] or high[1]:
            return None
        if not (_NUMBER.match(low[0]) and _NUMBER.match(high[0])):
            return None
        base = _unique_slot(f"num_{column}", ctx["taken"])
        ctx["taken"] |= {base, f"{base}_low", f"{base}_high", f"{base}_op"}
        low_token = _TOKEN.format(f"{ctx['seq']}x{column}_low")
        high_token = _TOKEN.format(f"{ctx['seq']}x{column}_high")
        replacement = exp.Between(
            this=lhs.copy(),
            low=exp.Literal.string(low_token),
            high=exp.Literal.string(high_token),
        )
        before = replacement.sql(dialect="mysql")
        after = (
            f"{lhs_sql} BETWEEN {{{base}_low}} AND {{{base}_high}}"
        )
        return {
            "before": before,
            "after": after,
            "params": {
                f"{base}_low": _slot_spec(info, column, {"type": "numeric"}),
                f"{base}_high": _slot_spec(info, column, {"type": "numeric"}),
            },
            "values": {f"{base}_low": low[0], f"{base}_high": high[0]},
            "declares": {f"{base}_low": base, f"{base}_high": base},
            "replacement": replacement,
        }
    if is_in:
        raw = [_literal_value(item) for item in node.expressions]
        if not raw or any(item is None for item in raw):
            return None
        if len({item[1] for item in raw if item}) != 1:
            return None
        original = ",".join(str(item[0]) for item in raw if item)
        replacement = exp.In(this=lhs.copy(), expressions=[exp.Literal.string(token)])
        before = replacement.sql(dialect="mysql")
        param, declare = _name_for(kind, info, column)
        # 一个实体槽里比较了多个对象（`r.id IN ('路线1','路线2')`）= 多对象题：
        # 声明成 multi_route，模板才敢接"两条路分别是几车道"这种问题（P0-6）。
        if len([item for item in raw if item]) > 1 and declare == "route":
            declare = "multi_route"
        after = f"{lhs_sql} IN ('{{{param}}}')"
        return {
            "before": before,
            "after": after,
            "params": {param: _slot_spec(info, column, {"type": kind, "options": info.get("options") or [], "multi": True})},
            "values": {param: original},
            "declares": {param: declare},
            "replacement": replacement,
        }
    right = _literal_value(node.expression if hasattr(node, "expression") else None)
    if right is None:
        return None
    value, quoted = right
    if kind == "numeric":
        # 引号包着的数字也算数字：LLM 生成 SQL 时经常写 `p.area > '10'`（MySQL 照跑），
        # 按"必须裸数字"卡会把可换的阈值留成固定值。值是数字文本就够，引号只是写法。
        if not _NUMBER.match(value):
            return None
        key = (column, op, value)
        base = ctx["by_predicate"].get(key) or ""
        if not base:
            base = _unique_slot(f"num_{column}", ctx["taken"])
            ctx["taken"] |= {base, f"{base}_op"}
            ctx["by_predicate"][key] = base
        replacement = exp.EQ(this=lhs.copy(), expression=exp.Literal.string(token))
        before = replacement.sql(dialect="mysql")
        after = f"{lhs_sql} {{{base}_op}} {{{base}}}"
        return {
            "before": before,
            "after": after,
            "params": {
                base: _slot_spec(info, column, {"type": "numeric"}),
                f"{base}_op": {
                    "label": f"{info.get('label') or column}比较符",
                    "type": "op",
                    "required": True,
                    "options": [
                        {"label": item, "value": item} for item in (">", ">=", "<", "<=", "=")
                    ],
                },
            },
            "values": {base: value, f"{base}_op": op},
            "declares": {base: base},
            "replacement": replacement,
        }
    if kind == "bool_image":
        # 原始采集图非空 = 有图（"problem_url IS NULL OR problem_url = ''" 就是无图）
        if op == "=" and value in {"", "NULL", "null"}:
            negative = True
        elif op == "<>" and value in {"", "NULL", "null"}:
            negative = False
        else:
            return None
        replacement = exp.EQ(this=lhs.copy(), expression=exp.Literal.string(token))
        before = replacement.sql(dialect="mysql")
        return {
            "before": before,
            "after": "{has_image}",
            "params": {
                "has_image": {
                    "label": "有没有采集图片",
                    "type": "enum",
                    "required": True,
                    "column": column,
                    "options": [
                        {"label": "有图片", "value": "1"},
                        {"label": "无图片", "value": "0"},
                    ],
                    # 值 → SQL 片段：以整段谓词为槽，避免把"IS NULL OR = ''"拆坏
                    "fragments": {
                        "0": _image_cond(lhs_sql, False),
                        "1": _image_cond(lhs_sql, True),
                    },
                }
            },
            "values": {"has_image": "0" if negative else "1"},
            "declares": {"has_image": "has_image"},
            "replacement": replacement,
        }
    if kind == "time":
        if not quoted or not _DATETIME.match(value):
            return None
        param = f"time_{'end' if op in {'<', '<='} else 'start'}"
        replacement = exp.EQ(this=lhs.copy(), expression=exp.Literal.string(token))
        before = replacement.sql(dialect="mysql")
        after = f"{lhs_sql} {op} '{{{param}}}'"
        return {
            "before": before,
            "after": after,
            "params": {param: _slot_spec(info, column, {"type": "time", "source": "months", "op": op})},
            "values": {param: value},
            "declares": {param: "time_range"},
            "replacement": replacement,
        }
    if kind in {"entity", "enum", "text_slot"}:
        if not quoted:
            return None
        param, declare = _name_for(kind, info, column)
        lhs_node = lhs.copy()
        lhs_sql_now = lhs_sql
        value_now = value
        normalize = str(info.get("normalize_to_id") or "")
        if normalize:
            # 路线槽统一成 id 列：`r.short_name = '宝安大道西乡段'` → `r.id = '<id>'`
            resolved = _route_id_by_name(value)
            if not resolved:
                return None  # 名字对不上唯一路线就别挖（宁可少挖，不可错挖）
            table_alias = str(lhs.table or "")
            expr = f"{table_alias}.{normalize}" if table_alias else normalize
            lhs_node = sqlglot.parse_one(expr, read="mysql")
            lhs_sql_now = expr
            value_now = resolved
            column = normalize
            info = {**info, "column": normalize, "form": "value"}
        replacement = exp.EQ(this=lhs_node, expression=exp.Literal.string(token))
        before = replacement.sql(dialect="mysql")
        after = f"{lhs_sql_now} = '{{{param}}}'"
        return {
            "before": before,
            "after": after,
            "params": {param: _slot_spec(info, column, {"type": kind, "options": info.get("options") or []})},
            "values": {param: value_now},
            "declares": {param: declare},
            "replacement": replacement,
        }
    return None


def _route_id_by_name(name: str) -> str:
    """路线名/编码 → 路线 id（唯一命中才认，多命中或没命中都返回空）。"""
    target = str(name or "").strip()
    if not target:
        return ""
    hits = [
        row
        for row in _route_rows()
        if target in {str(row.get("code") or ""), str(row.get("short_name") or ""), str(row.get("full_name") or "")}
    ]
    if len(hits) != 1:
        return ""
    return str(hits[0].get("id") or "")


_ROUTE_ROWS_CACHE: tuple[float, list[dict[str, Any]]] | None = None


def _route_rows() -> list[dict[str, Any]]:
    """路线台账（缓存 5 分钟）：挖槽时把路线名解析成 id 用。"""
    global _ROUTE_ROWS_CACHE
    now = time.time()
    if _ROUTE_ROWS_CACHE and now - _ROUTE_ROWS_CACHE[0] < 300.0:
        return _ROUTE_ROWS_CACHE[1]
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT id, code, short_name, full_name FROM basic_facilities_routes WHERE del_flag = 0",
        )["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "route_rows_failed", exc)
        if _ROUTE_ROWS_CACHE:
            # 有旧缓存（哪怕过期）就接着用，别打断正在跑的挖槽
            return _ROUTE_ROWS_CACHE[1]
        # 没旧缓存就不写缓存：一次失败不该把"没有路线"钉死 5 分钟，下次调用重试
        return []
    _ROUTE_ROWS_CACHE = (now, rows)
    return rows


def _image_cond(lhs_sql: str, has_image: bool) -> str:
    if has_image:
        return f"({lhs_sql} IS NOT NULL AND {lhs_sql} <> '')"
    return f"({lhs_sql} IS NULL OR {lhs_sql} = '')"


def _name_for(kind: str, info: dict[str, Any], column: str) -> tuple[str, str]:
    """参数名 + 它对应计划里的哪种条件（route / damage_type / time_range / 列名…）。"""
    role = str(info.get("role") or column)
    if role == "route":
        return "route_id", "route"
    if role == "damage_type":
        return "damage_type", "damage_type"
    return column, role


# ── 单条 SQL 挖掘 ───────────────────────────────────────────────────────────
_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Merge)


def question_numbers(question: str) -> set[float]:
    """问法里出现的**数字**（含桩号写法：`k1+802` → 1802）。

    用途见 `unmined_question_numbers`：用户点了的数字必须挖成槽。
    先把桩号整段掩掉再取普通数字——不然 "k1+802" 会额外贡献 1 和 802 两个假数字，
    而它们不是用户要的值，会把 `SUBSTRING_INDEX(x,'+',1)` 这类结构常量误判成"用户值"。
    """
    text = str(question or "")
    out: set[float] = set()
    for match in re.finditer(r"[kK]\s*(\d+)\s*\+\s*(\d+)", text):
        out.add(float(int(match.group(1)) * 1000 + int(match.group(2))))
    masked = re.sub(r"[kK]\s*\d+\s*\+\s*\d+", " ", text)
    for match in re.finditer(r"\d+(?:\.\d+)?", masked):
        try:
            out.add(float(match.group(0)))
        except ValueError:
            continue
    return out


def unmined_question_numbers(
    tree: Any, question: str, fixed_names: set[str]
) -> list[tuple[str, str]]:
    """挖完槽后**还以字面值留在 SQL 里**的"用户数字" → [(数字, 上下文)]。

    这是纯 AST 挖槽的固有盲区：谓词里的值能挖（`p.area > 10` → 数值槽），
    但**表达式里的字面值挖不动**——典型是 `ORDER BY ABS(p.pile_distance - 1802)`：
    1802 是用户问的桩号（k1+802），模板却把它写死；换成 k1+000 复用，排序基准仍是 1802，
    查出来"看着对、其实错"，而且不会报错（ABS 后是正数）。

    判据只认一件事：**这个数字在用户问法里出现过**。结构常量（`ROUND(…, 2)`、
    `DECIMAL(10,6)`、`/ 1000` 换算、LIMIT）永远不会等于用户点过的值，所以不会误伤；
    反过来，"每 100 米一段"里的 100 属于用户指定的分档粒度，命中就说明它同样挖漏了。
    两侧都按数值比（问法里的 `0.7` 与 SQL 里的 `0.7` 是同一个数）。
    """
    wanted = question_numbers(question)
    if not wanted or tree is None:
        return []
    limit_node = tree.args.get("limit")
    out: list[tuple[str, str]] = []
    for node in tree.find_all(exp.Literal):
        if not node.is_number:
            continue
        text = str(node.this)
        try:
            value = float(text)
        except ValueError:
            continue
        if value not in wanted:
            continue
        ancestor: Any = node
        skip = False
        while ancestor is not None:
            if ancestor is limit_node or isinstance(ancestor, exp.DataType):
                skip = True
                break
            ancestor = ancestor.parent
        if skip:
            continue
        owner = ""
        current: Any = node
        while current is not None:
            if isinstance(
                current, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
            ):
                left = current.this
                column = left if isinstance(left, exp.Column) else left.find(exp.Column)
                owner = str(column.name) if isinstance(column, exp.Column) else ""
                break
            if isinstance(current, exp.Select):
                break
            current = current.parent
        if owner and owner in fixed_names:
            continue
        context = str(node.parent.sql(dialect="mysql"))[:60] if node.parent else text
        out.append((text, context))
    return out


_EXTRA_SLOTS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_EXTRA_SLOTS_TTL = 24 * 3600.0


def _table_fields_text(sql: str, limit: int = 60) -> str:
    """这条 SQL 涉及的表和字段描述（给语义挖槽看）：表.列 · 中文名 · 注释 · 枚举值。

    纯本地取字段目录，零 LLM 成本；只列这条 SQL 真正用到的表（CTE 也算它的物理表）。
    """
    try:
        tree = sqlglot.parse_one(str(sql or ""), read="mysql")
    except Exception:  # noqa: BLE001
        return "（SQL 解析不出来）"
    names: list[str] = []
    for table in tree.find_all(exp.Table):
        name = str(table.name or "")
        if name and name not in names:
            names.append(name)
    for source in _cte_source_tables(tree).values():
        if source and source not in names:
            names.append(source)
    lines: list[str] = []
    for name in names:
        entry = schema_catalog.table_entry("mysql", name)
        if not entry:
            continue
        for column in entry.get("columns") or []:
            code_map = column.get("code_map") or {}
            hint = ""
            if code_map:
                hint = "（枚举：" + "，".join(
                    f"{code}={text}" for code, text in list(code_map.items())[:8]
                ) + "）"
            lines.append(
                f"- {name}.{column.get('name')}：{column.get('label') or ''}"
                f"（{str(column.get('comment') or '')[:60]}）{hint}"
            )
            if len(lines) >= limit:
                return "\n".join(lines)
    return "\n".join(lines) or "（没有可用的字段描述）"


def _plan_text(question: str) -> str:
    """这句问法的语义规划结果（给语义挖槽看）：主体/形态/条件/指标。

    直接复用 nl2sql 的规划（它有缓存，同一句不重复花钱）；规划不出来就如实说没有。
    """
    try:
        from . import nl2sql

        plan = nl2sql.build_query_plan(str(question or ""), "mysql")
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "classify_plan_failed", exc, question=str(question)[:60])
        return "（规划不出来）"
    if not plan:
        return "（规划不出来）"
    slots = plan.get("slots") or {}
    brief = {
        "subject": plan.get("subject"),
        "shape": plan.get("shape"),
        "route": slots.get("route"),
        "damage_type": slots.get("damage_type"),
        "time_start": slots.get("time_start"),
        "time_end": slots.get("time_end"),
        "pile_start": slots.get("pile_start"),
        "pile_end": slots.get("pile_end"),
        "semantic_conditions": slots.get("semantic_conditions"),
        "required_conditions": plan.get("required_conditions"),
    }
    return json.dumps(brief, ensure_ascii=False, default=str)[:1200]


def _resolve_any_column(
    hint: str, index: dict[str, list[tuple[str, dict[str, Any]]]], tables: list[str]
) -> tuple[str, dict[str, Any]] | None:
    """按列名找"这条 SQL 里出现的那张表"的列（LLM 给的列名一律回目录核）。"""
    name = str(hint or "").strip().lower()
    if not name:
        return None
    candidates = index.get(name) or []
    for table, col in candidates:
        if table in tables:
            return table, col
    return candidates[0] if len(candidates) == 1 else None


def _extra_slot_spec(
    table: str, col: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any] | None:
    """给"语义上该挖的字面值"造槽规格：类型/选项/绑定方式**全按字段目录定**，不采信模型给的选项。

    编错选项比不挖更糟（换值时会填进库里根本没有的值），所以只信目录：
    有 code_map → 枚举槽；数值列 → 数值槽（阈值可换）；外键列 → 实体槽。三类都不是 → 不挖。
    """
    name = str(col.get("name") or "")
    roles = set(col.get("roles") or [])
    label = str(col.get("label") or proposal.get("param_label") or name)
    code_map = col.get("code_map") or {}
    if code_map:
        return {
            "label": label,
            "type": "enum",
            "required": True,
            "column": name,
            "table": table,
            "options": [
                {"label": str(text), "value": str(code)} for code, text in code_map.items()
            ],
            "confirmed_by": "domain",
            "declare": name,
        }
    join = col.get("join")
    if isinstance(join, dict) and join.get("table") and join.get("column"):
        return {
            "label": label,
            "type": "entity",
            "required": True,
            "column": name,
            "table": table,
            "role": _role_for_table(str(join["table"]), name),
            "form": "value",
            "options": _entity_options(
                str(join["table"]), str(join.get("on") or ""), str(join["column"])
            ),
            "multi": True,
            "confirmed_by": "domain",
            "declare": name,
        }
    if "numeric" in roles:
        return {
            "label": label,
            "type": "numeric",
            "required": True,
            "column": name,
            "table": table,
            "confirmed_by": "domain",
            "declare": name,
        }
    return None


def _apply_extra_slots(
    text: str,
    proposals: list[dict[str, Any]],
    params: dict[str, Any],
    values: dict[str, str],
    declares: dict[str, str],
    tables: list[str],
) -> tuple[str, dict[str, Any], dict[str, str], dict[str, str], list[str]]:
    """把 LLM 判定的"该挖的字面值"精确替换成占位符（代码执行，模型不碰 SQL）。

    安全性：列/选项回字段目录核；替换后**必须能按原值回填出替换前的 SQL**（逐字，去空白），
    做不到就整条放弃 → 调用方退回"宁可不沉淀"的兜底。
    """
    original = text
    index = _column_index()
    # Wren 产出的 SQL 外层是 CTE（`FROM disease AS d JOIN route AS r`），
    # 光看 `ctx["tables"]` 只有 CTE 名，找不到 `ai_problem.routes_id` 这种真列 →
    # 把 CTE 底下正对着的物理表也放进允许集合（"一头还是两头"那条就是 CTE 形态）。
    try:
        tree_now = sqlglot.parse_one(text, read="mysql")
        cte_sources = _cte_source_tables(tree_now)
        tables = list(tables) + [source for source in cte_sources.values() if source not in tables]
    except Exception:  # noqa: BLE001
        pass
    applied: list[str] = []
    undo: list[tuple[str, str]] = []
    for item in proposals:
        if not isinstance(item, dict):
            continue
        fragment = str(item.get("fragment") or "").strip()
        value = str(item.get("value") or "").strip()
        if not fragment or not value or fragment not in text:
            continue
        found = _resolve_any_column(str(item.get("column_hint") or ""), index, tables)
        if not found:
            continue
        table, col = found
        spec = _extra_slot_spec(table, col, item)
        if not spec:
            continue
        base = f"num_{col.get('name')}" if spec.get("type") == "numeric" else str(col.get("name"))
        # 同一列已经有过参数就**复用那个参数名**：实测"一头还是两头"那条，外层用 `{route_id}`、
        # 子查询里把同一个路线 id 写死了——两处必须是**同一个槽**，否则复用时一个填新值、
        # 一个还是老值，等于没修。
        reused = next(
            (
                existing
                for existing, existing_spec in params.items()
                if str((existing_spec or {}).get("column") or "")
                == str(col.get("name") or "")
                and str((existing_spec or {}).get("table") or "") == table
            ),
            "",
        )
        if reused and str(values.get(reused) or "") != value:
            # 同一个槽却被写成了**不同的值**：不是"漏挖同一个参数"，是两码事，别硬套
            continue
        name = reused
        if not name:
            name = base
            suffix = 2
            while name in params:
                name = f"{base}_{suffix}"
                suffix += 1
        pattern = re.compile(r"(?<![\w.])" + re.escape(value) + r"(?![\w.])")
        if not pattern.search(fragment):
            continue
        replaced = pattern.sub("{" + name + "}", fragment)
        text = text.replace(fragment, replaced, 1)
        undo.append((replaced, fragment))
        if not reused:
            params[name] = spec
            values[name] = value
            declares[name] = str(spec.get("declare") or "")
        applied.append(name)
    if not applied:
        return original, params, values, declares, []
    # 等价性：把刚做的替换**反向还原**必须得到替换前的 SQL（不能拿"回填整个 SQL"来验：
    # 复用已有参数时，SQL 里本来就有 `{route_id}` 占位符，回填它当然会变样——那是误判）。
    restored = text
    for replaced, fragment in reversed(undo):
        restored = restored.replace(replaced, fragment, 1)
    if re.sub(r"\s+", "", restored) != re.sub(r"\s+", "", original):
        return original, params, values, declares, []
    return text, params, values, declares, applied


def llm_extra_slots(
    question: str,
    sql: str,
    suspicious: list[tuple[str, str]],
    hints: list[str] | None = None,
) -> list[dict[str, Any]]:
    """语义补漏：把"用户点了、却没挖成槽"的字面值交给模型判断该不该挖（带缓存、可关）。

    两个触发口（都由调用方决定）：
      · 确定性挖槽后**还有用户数字留在 SQL 里**（`unmined_question_numbers`）；
      · **审核给了意见**（`hints`：审核说"这处参数没覆盖/写死了"）——这一口很重要，
        因为漏挖不一定带数字（实测"一头还是两头"那条把路线 id 写死在相关子查询里，
        问法里一个数字都没有，只有审核看得出来）。
    模型只做语义分类，SQL 的替换与校验全在代码里
    （见 docs/模板挖槽位与LLM审核逻辑重构方案.md §2.1）。
    """
    if not bool(getattr(settings, "template_mine_use_llm", True)):
        return []
    key = hashlib.sha1(
        json.dumps([question, sql, suspicious, hints or []], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    cached = _EXTRA_SLOTS_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _EXTRA_SLOTS_TTL:
        return cached[1]
    from . import prompts
    from .llm import LLMError, chat_json, role_model

    fields_text = _table_fields_text(sql)
    plan_text = _plan_text(question)
    try:
        data = chat_json(
            prompts.messages(
                "templatize_classify",
                question=str(question or "")[:300],
                sql=str(sql or "")[:4000],
                suspicious="；".join(f"{value}（{where}）" for value, where in suspicious[:5]),
                hints="；".join(str(item)[:200] for item in (hints or [])[:3]) or "（无）",
                # 提示词要"表和字段描述 + 语义规划 plan"两段上下文（模型靠它们判断
                # 这个值属于哪一列、用户到底问了什么）；字段描述本地零成本，
                # plan 走 nl2sql 自己的规划缓存（同一句只算一次）。
                table_fields=fields_text,
                plan=plan_text,
            ),
            model=role_model("semantic"),
        )
    except (LLMError, Exception) as exc:  # noqa: BLE001
        log_error("templatize", "llm_extra_slots_failed", exc, question=str(question)[:60])
        return []
    slots = [item for item in (data or {}).get("slots") or [] if isinstance(item, dict)]
    if len(_EXTRA_SLOTS_CACHE) >= 500:
        _EXTRA_SLOTS_CACHE.clear()
    _EXTRA_SLOTS_CACHE[key] = (now, slots)
    log_event(
        "templatize",
        "INFO",
        "llm_extra_slots",
        proposed=len(slots),
        question=str(question)[:50],
    )
    return slots


def _precheck(tree: Any, sql: str, statements: int) -> str:
    """预检（方案 §2.2）。返回空串=可以挖，否则返回拒绝原因。"""
    cfg = _cfg()
    if statements != 1:
        return "多语句"
    if not isinstance(tree, exp.Select):
        return "不是单条只读 SELECT"
    if any(tree.find(node) for node in _FORBIDDEN):
        return "含写操作/非查询语句"
    lowered = sql.lower()
    for marker in cfg.get("dynamic_markers") or []:
        if str(marker) in lowered:
            return f"运行时动态分界（{marker}）"
    known = {
        str(table.get("table")).lower()
        for table in schema_catalog.field_catalog("mysql").get("tables") or []
    }
    prefix = str(cfg.get("wren_table_prefix") or "wren_src_")
    # CTE 名不是物理表：Wren 产出的物理 SQL 一律形如 `WITH disease AS (...)`，
    # 不排掉就会报"表不在白名单：disease"，人工确认过的这类示例永远进不了模板库
    # （docs/推荐模板链路Bug清单.md P0-4）。
    cte_names = {str(cte.alias_or_name or "").lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = str(table.name or "").lower()
        if not name:
            continue
        if name in cte_names:
            continue
        if name.startswith(prefix):
            return "Wren 逻辑表（第一期不做模板化）"
        if name not in known:
            return f"表不在白名单：{name}"
    return ""


def mine_sql(
    question: str,
    sql: str,
    kind: str = "mysql",
    hints: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """把一条成功 SQL 挖成参数化骨架。返回 (骨架, 拒绝原因)。"""
    if str(kind) != "mysql":
        return None, "只模板化 mysql 物理 SQL"
    effective_sql = str(sql or "")
    from_wren = False
    if "wren_src_" in effective_sql.lower():
        # Wren MDL-SQL：先编译成物理 SQL 再挖（模板必须能直接跑物理库；执行/验证也都走物理）
        compiled = _compile_wren(kind, effective_sql)
        if not compiled:
            return None, "Wren MDL-SQL 编译不出物理 SQL（先在 Wren 侧确认这条能编译）"
        effective_sql = compiled
        from_wren = True
    try:
        statements = sqlglot.parse(effective_sql, read="mysql")
    except Exception as exc:  # noqa: BLE001
        return None, f"解析失败：{exc}"
    if not statements:
        return None, "解析失败：空语句"
    tree = statements[0]
    reason = _precheck(tree, effective_sql, len(statements))
    if reason:
        return None, reason
    where = tree.args.get("where")
    conjuncts = _and_children(where.this) if where is not None else []
    if conjuncts is None:
        return None, "WHERE 里有 OR 逻辑，参数位置不稳定"
    ctx = _ctx_for(tree)
    replacements: list[tuple[str, str]] = []
    params: dict[str, Any] = {}
    values: dict[str, str] = {}
    declares: dict[str, str] = {}
    skipped: list[str] = []
    # "挖不动但会原样留在这条 SQL 里"的条件：必须在 match.fixed 里声明，
    # 否则模板会去接不含这个条件的题（见 _fixed_condition_of）
    fixed_extra: dict[str, str] = {}
    for index, node in enumerate(conjuncts):
        mined = _mine_predicate(node, ctx)
        if mined is None:
            # 明确不可泛化的条件（LIKE/自由文本/子查询）→ 整条 SQL 不模板化，写清原因
            return None, f"有不可泛化的条件：{node.sql(dialect='mysql')[:60]}"
        if mined.get("skip"):
            # 单个谓词挖不动：只把它留作固定条件，其余谓词正常挖槽
            # （P1-4：以前一个 CAST/JSON 谓词就把整条 SQL 丢给模型挖槽）
            skipped.append(node.sql(dialect="mysql")[:60])
            key, value = _fixed_condition_of(node.sql(dialect="mysql"))
            if key:
                fixed_extra[key] = value
            continue
        if mined.get("fixed"):
            continue
        # 真正把 AST 里的谓词换成哨兵表达式，渲染后再做文本替换（值 → 占位符）
        node.replace(mined["replacement"])
        replacements.append((str(mined["replacement"].sql(dialect="mysql")), str(mined["after"])))
        params.update(mined["params"])
        values.update(mined["values"])
        declares.update(mined["declares"])
    # SELECT 里的 CASE WHEN 阈值也要挖成槽（"面积大于 10 的病害有多少处/占多少比例"这类题）：
    # 阈值写在投影里（`SUM(CASE WHEN p.area > 10 THEN 1 ELSE 0 END)`），只挖 WHERE 的话这个
    # 数字会**原样固定**在 SQL 里 → 同一句问法换个阈值（5 平方米）就复用不了，而记录的
    # conditions 里也没有 num_area，模板还会去接不含阈值的题（实测 auto_8b8f1e46bb2dfb18）。
    # 判据和 WHERE 完全一样（列在字段目录里是 numeric 角色、值能当数字看），所以"整型/浮点
    # 列的阈值都可以换"；挖不动的分支原样保留，不因为一个 CASE 分支把整条 SQL 判死。
    for case in tree.find_all(exp.Case):
        if case.args.get("this") is not None:
            # 简单 CASE（`CASE b.disease_categories_type WHEN '0' THEN '路面病害' …`）：
            # sqlglot 把 ifs 里的这一项当成**取值**（If.this 是 Literal），不是能挖的布尔条件 → 跳过
            continue
        for branch in case.args.get("ifs") or []:
            condition = getattr(branch, "this", None)
            if not isinstance(condition, exp.Condition):
                continue
            mined = _mine_predicate(condition, ctx)
            if not mined or mined.get("skip") or mined.get("fixed"):
                continue
            # **只挖数值阈值**：`SUM(CASE WHEN p.area > 10 THEN 1 ELSE 0 END)` 这种阈值是用户
            # 会换的参数；而编码列的比较（`CASE WHEN up_or_down = '1' THEN '上行' …` 的码→中文
            # 映射、`CASE WHEN problem_type = '泛油' THEN 1 …` 的分类型列名）挖成槽反而错：
            # 换掉它等于把标签映射改坏，而且同一列两个分支会撞同一个槽名。
            if any(
                str(spec.get("type") or "") not in {"numeric", "op"}
                for spec in (mined.get("params") or {}).values()
            ):
                continue
            condition.replace(mined["replacement"])
            replacements.append(
                (str(mined["replacement"].sql(dialect="mysql")), str(mined["after"]))
            )
            params.update(mined["params"])
            values.update(mined["values"])
            declares.update(mined["declares"])
    # 别名里写死的阈值也跟着参数化：`AS 面积大于10平方米病害数` → `AS 面积大于{num_area}平方米病害数`。
    # 绑定时整条 SQL 一起替换，卡片的表头才不会写着"大于 10"、实际按用户给的 5 去查。
    _parameterize_aliases(tree, values)
    if skipped:
        # 还有"挖不动的谓词"时，给便宜模型一次机会把它也挖出来（prompts/templatize_sql.md）；
        # 模型也挖不动就按规则结果入库——那段条件作为固定条件保留在 SQL 里，不丢。
        fallback, reason = _llm_skeleton(question, sql, kind)
        if fallback:
            # 模型给的那份也要带上"固定条件声明"，否则同样的口径漏洞又回来了
            fallback["fixed_extra"] = {**(fallback.get("fixed_extra") or {}), **fixed_extra}
            return fallback, ""
        log_event("templatize", "INFO", "predicate_kept_fixed", reason=reason, sql=sql[:60])
    if len(params) > int(_cfg().get("max_slots") or 8):
        return None, "槽位过多"
    # LIMIT：落在白名单里的当"取前 N 名"槽；平台默认行数上限（500 之类）当固定结构保留
    limit = tree.args.get("limit")
    # 分档粒度按**形状**挖（确定性、零模型、不看问法措辞）：
    # `FLOOR((pile_distance % 1000) / 100) * 100` 里的 100 是用户说的"每 100 米一段"，
    # 写死就等于换粒度时静默答错；同一段里的 `% 1000` 是公里→米换算，必须原样留着。
    # 走和 LIMIT 同一套"树里换 token → 文本换占位符"（`{...}` 不是合法 SQL，不能在树上直接写）。
    # 单位换算的模数（`(col % 1000) / N` 里的 1000）：它**不是**粒度，必须留着。
    # 判据是形状——这个 Mod 的祖先里有没有除法；没有除法的裸取模才是分档（`GROUP BY col % 100`）。
    div_moduli: set[str] = set()
    for item in tree.find_all(exp.Mod):
        modulus = getattr(getattr(item, "expression", None), "this", None)
        if modulus is None:
            continue
        parent = item.parent
        while parent is not None and not isinstance(parent, exp.Select):
            if isinstance(parent, (exp.Div, exp.IntDiv)):
                div_moduli.add(str(modulus))
                break
            parent = parent.parent
    candidates: list[tuple[Any, str]] = []
    for cls in (exp.Div, exp.IntDiv):  # `/ N` 与 `DIV N`
        for node in list(tree.find_all(cls)):
            right = node.expression
            if not (isinstance(right, exp.Literal) and right.is_number):
                continue
            if node.this.find(exp.Column) is None:
                continue  # 分母上的数字要配"列"才算分档
            candidates.append(
                (right, str((node.this.find(exp.Column)).name or ""))
            )
    for node in list(tree.find_all(exp.Mod)):  # 裸 `col % N` / `MOD(col, N)`
        right = node.expression
        if not (isinstance(right, exp.Literal) and right.is_number):
            continue
        if node.this.find(exp.Column) is None:
            continue
        if str(right.this) in div_moduli:
            continue
        candidates.append((right, str((node.this.find(exp.Column)).name or "")))
    for right, column in candidates:
        value = str(right.this)
        try:
            if float(value) <= 1:
                continue
        except ValueError:
            continue
        name = f"num_{column}_bucket" if column else "num_bucket"
        # 同一个粒度值在同一段表达式里出现多次（`/ 100) * 100`、`+ 100`）→ **共用一个槽**：
        # 各建一个槽的话卡片上会出现 4 个一模一样的"分档粒度"，还容易填得不一致。
        reused = next(
            (
                existing
                for existing, existing_spec in params.items()
                if str((existing_spec or {}).get("declare") or "") == "bucket"
                and str(values.get(existing) or "") == value
            ),
            "",
        )
        if reused:
            name = reused
        else:
            suffix = 2
            while name in params:
                name = f"num_{column}_bucket_{suffix}"
                suffix += 1
        token = _TOKEN.format(f"bucket{column or 'size'}")
        right.replace(exp.Literal.string(token))
        replacements.append((f"'{token}'", "{" + name + "}"))
        if not reused:
            params[name] = {
                "label": "分档粒度（米）",
                "column": column or "pile_distance",
                "type": "numeric",
                "required": True,
                "confirmed_by": "domain",
                "declare": "bucket",
            }
            values[name] = value
            declares[name] = "bucket"
    # 同一分档粒度的**其它出现位置**（`FLOOR(...) * 100`、`... + 100`）也要跟着换，
    # 否则 SQL 里还留着这个用户值 → 会被"用户数字没挖干净"的闸拦下（实测就是这么拦的）。
    for node in list(tree.find_all(exp.Mul)) + list(tree.find_all(exp.Add)):
        if node.this.find(exp.Column) is None:
            continue
        for side, other in ((node.expression, node.this), (node.this, node.expression)):
            if not (isinstance(side, exp.Literal) and side.is_number):
                continue
            pair = next(
                (
                    (existing, spec)
                    for existing, spec in params.items()
                    if str((spec or {}).get("declare") or "") == "bucket"
                    and str(values.get(existing) or "") == str(side.this)
                ),
                None,
            )
            if not pair or other.find(exp.Column) is None:
                continue
            token = _TOKEN.format(f"bucket{pair[0]}")
            side.replace(exp.Literal.string(token))
            replacements.append((f"'{token}'", "{" + pair[0] + "}"))
    # 时间粒度（按年/月/周/日/小时分组）：`DATE_FORMAT(col, '%Y-%m')` 的**格式串**就是粒度。
    # 词表封闭（5 种），所以能做成枚举槽；复用时由计划侧的 group_by 填（见 _plan_param_value），
    # 这样"按周看"不会套到"按月"的模板上（以前 `_ast_profile` 把任何 date_format 都记成 month，
    # 按天/按小时/按周的模板会被按月的题命中，桶大小不对还静默答错）。
    time_bucket_group = ""
    for node in list(tree.find_all(exp.Func)):
        if not str(node.sql(dialect="mysql"))[:12].lower().startswith("date_format("):
            continue
        args = list(node.expressions)
        if len(args) < 2:
            # sqlglot 对 DATE_FORMAT 可能用命名参数（this=列 / format=格式串）而不是位置参数
            args = [node.args.get("this"), node.args.get("format")]
        if len(args) < 2 or not isinstance(args[1], exp.Literal):
            continue
        fmt = _TIME_BUCKET_ALIASES.get(str(args[1].this), str(args[1].this))
        if fmt not in _TIME_BUCKET_FORMATS:
            continue
        name = "time_bucket"
        token = _TOKEN.format("timebucket")
        args[1].replace(exp.Literal.string(token))
        replacements.append((f"'{token}'", "{" + name + "}"))
        column = args[0].find(exp.Column)
        params[name] = {
            "label": "时间粒度",
            "column": str(column.name) if isinstance(column, exp.Column) else "collection_time",
            "type": "enum",
            "required": True,
            "confirmed_by": "domain",
            "declare": "time_bucket",
            "options": [
                {"label": label, "value": value}
                for value, label in _TIME_BUCKET_FORMATS.items()
            ],
        }
        values[name] = fmt
        declares[name] = "time_bucket"
        # 粒度是这里按**格式串**判出来的，必须回填给下面的自画像：`_ast_profile` 拿到的是
        # 已经被换成占位符的文本（看不到 %d/%x-%v），只能一律记成 month——实测"按天/按周"的
        # 模板会被"按月"的题命中，桶大小不对还静默答错（docs/周数计算模式不一致…§1.2）。
        time_bucket_group = {
            "%Y": "year",
            "%Y-%m": "month",
            "%Y-%m-%d": "day",
            "%x-%v": "week",
            "%Y-%m-%d %H": "hour",
        }.get(fmt, "month")
    if limit is not None:
        if not isinstance(limit.expression, exp.Literal):
            return None, "LIMIT 不是常量"
        number = str(limit.expression.this)
        if not _NUMBER.match(number):
            return None, "LIMIT 不是数字"
        low, high = (_cfg().get("limit_range") or [1, 50])[:2]
        # 只有"排名类"（带 ORDER BY）的 LIMIT 才是"取前 N 名"；纯行数上限（如 LIMIT 1/500）
        # 是平台保护，算结构、不暴露给用户
        ranked = tree.args.get("order") is not None
        if ranked and int(low) <= float(number) <= int(high):
            token = _TOKEN.format("limit")
            limit.expression.replace(exp.Literal.string(token))
            replacements.append((f"LIMIT '{token}'", "LIMIT {top_n}"))
            params["top_n"] = {"label": "取前几名", "type": "int", "required": True, "range": [low, high]}
            values["top_n"] = number
            declares["top_n"] = "top_n"
    text = tree.sql(dialect="mysql")
    for before, after in replacements:
        if before not in text:
            return None, "占位替换没对上（结构太复杂）"
        text = text.replace(before, after, 1)
    if _TOKEN.format("") in text or "zzslotxx" in text:
        return None, "还有没替掉的占位"
    # 用户点过的数字必须挖成槽：表达式里的字面值（`ABS(p.pile_distance - 1802)`）挖不动，
    # 留着就是"换值复用静默答错"的模板 —— 宁可不沉淀，也不许悄悄答错（见 unmined_question_numbers）。
    fixed_names = set(_cfg().get("fixed_columns") or []) | set((fixed_extra or {}).keys())
    unmined = unmined_question_numbers(tree, question, fixed_names)
    if unmined or hints:
        # 补一层**语义**：确定性的 AST 挖槽看不懂"这个数字对应问法里的哪个值"
        # （`ABS(p.pile_distance - 1802)` 就是实测漏挖的典型）。让模型判该不该挖、属于哪一列，
        # 替换与校验仍由代码做；它给出方案后**再确定性复查一遍**，还没挖干净才真正拒绝沉淀。
        # 也接审核的意见（`hints`）：漏挖不一定带数字——"一头还是两头"那条把路线 id
        # 写死在相关子查询里，问法里一个数字都没有，只有审核看得出来。
        proposals = llm_extra_slots(question, text, unmined, hints)
        if proposals:
            tables = [str(name) for name in (ctx.get("tables") or [])]
            text, params, values, declares, applied = _apply_extra_slots(
                text, proposals, params, values, declares, tables
            )
            if applied:
                try:
                    tree = sqlglot.parse_one(text, read="mysql")
                except Exception:  # noqa: BLE001
                    tree = None
                if tree is not None:
                    unmined = unmined_question_numbers(tree, question, fixed_names)
    if unmined:
        detail = "、".join(f"{value}（{where}）" for value, where in unmined[:3])
        return None, f"问法里的数字没挖成槽：{detail}——换个值复用会静默答错"
    if not params:
        return None, "没有可替换的槽位（全表明细类）"
    # 全表明细：没有任何业务过滤（只剩 del_flag 之类的固定常量）
    if not declares:
        return None, "没有业务过滤条件"
    profile = _ast_profile(tree)
    if time_bucket_group:
        # 粒度以**格式串**判出来的为准（见上面 time_bucket 那段）：_ast_profile 看到的是占位符文本，
        # 只能记成 month，会把"按天/按周"的模板记成"按月"（换粒度复用就静默答错）。
        profile["group_by"] = [time_bucket_group]
    return (
        {
            # 问法要清掉"长对话补全留下的数量痕迹"（"…的 377 处'未识别'病害…"）：
            # 存的是模板问法，得能被下一句话复用，不能带着上一轮答案里的数字
            # （见 clean_template_question）
            "question": clean_template_question(question, text, values),
            "sql": text,
            "params": params,
            "values": values,
            "declares": declares,
            # 挖不动但留在 SQL 里的条件（"未识别"这类）：必须声明成 fixed，见 _fixed_condition_of
            "fixed_extra": fixed_extra,
            "fingerprint": fingerprint(text),
            "kind": "mysql",
            # 自画像直接从 AST 抽（不看问题措辞）
            "profile": profile,
            # 计数口径风控：见 _count_scope_risk
            "risk": _count_scope_risk(tree),
            # 验证/回环要用**物理 SQL**（Wren 那条编译后的）
            "effective_sql": effective_sql,
            "from_wren": from_wren,
        },
        "",
    )


def _compile_wren(kind: str, mdl_sql: str) -> str:
    """Wren MDL-SQL → 物理 SQL（编译不出来返回空串）。

    统一走 `nl2sql.physical_sql_of`：它除了编译 MDL-SQL，也能认出"本来就是 Wren 产出的
    物理 SQL"（那种 SQL 也带 wren_src_ 别名，直接编译会报"不是本项目的模型"）——
    否则这类已验证过的 SQL 根本挖不成模板。
    """
    from . import nl2sql

    try:
        return str(nl2sql.physical_sql_of(str(mdl_sql or ""), kind) or "")
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "wren_compile_failed", exc)
        return ""


def _count_scope_risk(tree: Any) -> str:
    """计数口径风控：**被计数的那个实体自己没有被过滤**、条件全挂在别的表上时标风险。

    实测那条"西乡段 7 月巡检了几次"：
    `SELECT COUNT(DISTINCT t.id) FROM ai_maintain_patrol t JOIN ai_problem p ON p.patrol_id = t.id
     ... WHERE r.short_name = '宝安大道西乡段' AND p.collection_time >= …`
    计数对象是巡查任务 t，但条件全在病害 p / 路线 r 上，而且 p 是 INNER JOIN ——
    "没查出病害的巡查"整条被剔掉，次数偏少。这种模板允许沉淀，但只落 candidate，
    人工核对口径后手动转正（docs/确认卡重复气泡耗时整改与模板库重置 §三）。
    """
    aggregates = [
        item for expression in tree.expressions or [] for item in expression.find_all(exp.AggFunc)
    ]
    if not aggregates or tree.args.get("group"):
        return ""
    if {str(item.key or "").lower() for item in aggregates} - {"count"}:
        return ""
    where = tree.args.get("where")
    where_sql = (where.sql(dialect="mysql") if where is not None else "").lower()
    filter_aliases = {str(name).lower() for name in re.findall(r"\b([a-z_][a-z0-9_]*)\.", where_sql)}
    counted: set[str] = set()
    for item in aggregates:
        target = item.this
        if isinstance(target, exp.Distinct):
            target = target.expressions[0] if target.expressions else target
        if isinstance(target, exp.Column) and target.table:
            counted.add(str(target.table).lower())
    if not counted or counted & filter_aliases:
        return ""  # 计数对象自己有过滤条件，或压根没点名（COUNT(*)），算正常
    if not filter_aliases:
        return ""
    return (
        "计数对象（"
        + "、".join(sorted(counted))
        + "）自己没有被过滤，条件全在别的表上，可能漏算（如'巡检几次'只统计到有病害的巡查）"
    )


def fingerprint(param_sql: str) -> str:
    """结构指纹：占位符与字面值全部抹平后按 AST 规范化取哈希。"""
    masked = _PLACEHOLDER.sub("?", param_sql)
    try:
        tree = sqlglot.parse_one(masked, read="mysql")
        for node in list(tree.find_all(exp.Literal)):
            node.replace(exp.Placeholder())
        normalized = tree.sql(dialect="mysql", normalize=True)
    except Exception:  # noqa: BLE001
        normalized = " ".join(masked.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _structure_signature(sql: str) -> tuple[Any, ...]:
    """结构签名：表 / JOIN 数 / 投影 / GROUP BY / WHERE 顶层条件数（字面值抹平）。"""
    tree = sqlglot.parse_one(sql, read="mysql")
    for node in list(tree.find_all(exp.Literal)):
        node.replace(exp.Placeholder())
    where = tree.args.get("where")
    conjuncts = _and_children(where.this) if where is not None else []
    tables = tuple(sorted({str(t.name).lower() for t in tree.find_all(exp.Table) if t.name}))
    selects = tuple(
        item.sql(dialect="mysql") for item in (tree.expressions or [])
    )
    group = tree.args.get("group")
    group_by = tuple(
        item.sql(dialect="mysql") for item in (group.expressions if group is not None else [])
    )
    return (
        tables,
        len(list(tree.find_all(exp.Join))),
        selects,
        group_by,
        len(conjuncts or []),
    )


def _rows_of(sql: str) -> list[dict[str, Any]]:
    from . import sql_engine

    return list(sql_engine.execute_sql("mysql", sql).get("rows") or [])


def _same_rows(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    if len(left) != len(right):
        return False
    key = lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)  # noqa: E731
    return sorted(map(key, left)) == sorted(map(key, right))


def _fill_fixed(sql: str, name: str, spec: dict[str, Any], value: str) -> str:
    fragments = spec.get("fragments") or {}
    if fragments:
        return sql.replace("{" + name + "}", str(fragments.get(str(value)) or fragments.get("0") or ""))
    if str(spec.get("type")) in {"entity", "enum", "time"}:
        return sql.replace("'" + "{" + name + "}" + "'", "'" + str(value).replace("'", "''") + "'")
    return sql.replace("{" + name + "}", str(value))


# ── 跨样本聚类与确认（方案 §2.5.4）────────────────────────────────────────────
_SUBJECT_BY_TABLE = {
    "ai_problem": "disease",
    "ai_maintain_patrol": "patrol",
    "basic_facilities_routes": "route",
    "basic_facilities_sections": "section",
    "basic_facilities_sections_pile_detail": "pile_point",
    "ai_asset_main": "asset",
    "ai_work_order": "work_order",
    "maintenance_work_order": "work_order",
}
# GROUP BY 的列 → 语义分组维度（和 prompts/query_semantic.md 的 group_by 取值对齐）
_GROUP_BY_NAMES = {
    "damage_type": "damage_type",
    "problem_type": "damage_type",
    # disease 模型的 ref_sql 把病害类型抽成 problem_type_name（LLM 写的 GROUP BY 常用它，
    # 或者包一层 COALESCE(..., '未识别')）：不认它 → 这条 SQL 的分组维度丢了，
    # 模板会被记成"无分组"，于是"哪几种病害类型最常见"这类题永远匹配不上
    # （docs/模板spec与现行语义漂移核查.md §3.1）。
    "problem_type_name": "damage_type",
    "up_or_down": "direction",
    "problem_status": "problem_status",
    "problem_source": "problem_source",
    "category": "category",
    "categories_type": "category",
    "routes_id": "route",
    "route_id": "route",
    "sections_id": "section",
    "section_id": "section",
    "luge_name": "luge_name",
    "device_code": "device",
    "vehicle_id": "device",
    "patrol_id": "patrol",
}
_AGG_METRIC = {
    "count": "count",
    "sum": "total",
    "avg": "avg",
    "max": "max",
    "min": "min",
}


def _metric_name(func: str, column: str) -> str:
    """聚合 → 指标名（和 capabilities/metrics.yaml 的口径命名保持一致）。"""
    base = _AGG_METRIC.get(func, func)
    key = str(column or "").lower()
    if func == "count":
        return "count"
    if not key:
        return base
    if key == "confidence":
        return "confidence_avg" if func in {"avg", "sum"} else f"confidence_{base}"
    return f"{base}_{key}"


def _ast_profile(tree: Any) -> dict[str, Any]:
    """从 SQL AST 抽"自画像"：shape / subjects / group_by / metrics。

    为什么不能拿第一条问题反推（老做法）：SQL 里明明 `COUNT(*) + SUM(area)`，
    按问题措辞反推只会得到 metrics=[count]，换参数复用时就判不了"这条模板也能答总面积"
    （docs/复杂模板匹配与推荐改造方案.md P0-2）。
    """
    metrics: list[str] = []
    plain_columns: list[str] = []
    agg_shapes: list[str] = []
    # 投影别名 → 真正的表达式：`GROUP BY 病害类型` 这种是按别名分组的，
    # 不解开就只能看到一个叫"病害类型"的列，判不出 group_by=damage_type
    alias_map: dict[str, Any] = {}
    for item in tree.expressions or []:
        alias = str(item.alias_or_name or "")
        if alias:
            alias_map[alias] = item.unalias() if hasattr(item, "unalias") else item
        aggregates = list(item.find_all(exp.AggFunc))
        if not aggregates:
            inner = item.unalias() if hasattr(item, "unalias") else item
            if isinstance(inner, exp.Column):
                plain_columns.append(str(inner.name))
            continue
        for node in aggregates:
            func = str(node.key or "").lower()
            target = node.this
            if isinstance(target, exp.Distinct):
                inner = target.expressions[0] if target.expressions else None
                if isinstance(inner, exp.Column):
                    # COUNT(DISTINCT x) 既是"去重计数"也是一个"计数"：
                    # 两个名字都登记，计划里问"几次/多少个"（metrics=count）才能对上
                    metrics.append("count")
                    metrics.append(f"distinct_{inner.name}")
                else:
                    metrics.append("count")
                    metrics.append("distinct")
                continue
            column = ""
            if isinstance(target, exp.Column):
                column = str(target.name)
            elif isinstance(target, exp.Star):
                column = ""
            metrics.append(_metric_name(func, column))
        # 聚合相除 = 占比/比例（`ROUND(SUM(CASE WHEN …) * 100 / COUNT(*), 2) AS 占比`）：
        # 语义层对这类问题给的指标是 `ratio`，不登记它的话模板记的是 count/total，
        # "占比题"永远匹配不上（实测 auto_116c1bf2553e1f32 挖出来后仍接不了自己的题）。
        if item.find(exp.Div) is not None and len(aggregates) >= 2:
            metrics.append("ratio")
    group = tree.args.get("group")
    group_by: list[str] = []
    # 分档对比（shape=bucket_compare）：分组表达式是一段 CASE（按面积/时间/桩号/置信度分两档）。
    # 它不是某一列的分布，也不该塞进 group_by（那是"按维度分组"的词表）。
    bucket_group = False
    for item in group.expressions if group is not None else []:
        if isinstance(item, exp.Column) and str(item.name) in alias_map:
            item = alias_map[str(item.name)]
        text = str(item.sql(dialect="mysql")).lower()
        if "case" in text and "up_or_down" in text:
            group_by.append("direction")
            continue
        if "case" in text and "problem_url" in text:
            group_by.append("image_presence")
            continue
        if "case" in text:
            bucket_group = True
            continue
        # 桩号分段（`CONCAT('K', FLOOR(p.pile_distance / 1000), …)` 这种按 100 米/1 公里分桶）：
        # 语义层对这个维度的取值就叫 pile_bucket，识别不出来就会记成"无分组的计数"，
        # 于是"按桩号分段统计"这类题永远匹配不上自己的模板
        if "pile_distance" in text or "pile_meter" in text:
            group_by.append("pile_bucket")
            continue
        if "date_format" in text:
            # 粒度由**格式串**决定（以前一律记成 month：按天/按小时/按周的模板会被
            # 按月的题命中，桶大小不对还静默答错）。格式串是封闭词表，按形状判即可。
            if re.search(r"%[uvwv]", text):
                group_by.append("week")
            elif "%H" in text:
                group_by.append("hour")
            elif "%d" in text:
                group_by.append("day")
            elif re.search(r"%[Yy]", text) and "%m" not in text:
                group_by.append("year")
            else:
                group_by.append("month")
            continue
        if "year(" in text:
            group_by.append("year")
            continue
        if "month(" in text:
            group_by.append("month")
            continue
        if "yearweek" in text:
            group_by.append("week")
            continue
        if "hour(" in text:
            group_by.append("hour")
            continue
        if re.search(r"\bdate\s*\(", text):
            group_by.append("day")
            continue
        column = item if isinstance(item, exp.Column) else item.find(exp.Column)
        if isinstance(column, exp.Column):
            name = str(column.name).lower()
            if name == "collection_time":
                group_by.append("month")
            elif name in _GROUP_BY_NAMES:
                group_by.append(_GROUP_BY_NAMES[name])
    subjects: list[str] = []
    for table in tree.find_all(exp.Table):
        name = str(table.name or "").lower()
        subject = _SUBJECT_BY_TABLE.get(name)
        if subject and subject not in subjects:
            subjects.append(subject)
    # 病害是最常见的主体：同时出现病害表和台账表时，"回答的是病害问题"
    if "disease" in subjects:
        subjects = ["disease"] + [item for item in subjects if item != "disease"]
    # 只有"取前 N 名"（LIMIT 在白名单里）才算 top；平台行数上限（LIMIT 500）是结构，
    # 分组查询仍然是 distribution（否则"各类型分布"会被判成 top，正反问法匹配不上模板）
    low, high = (_cfg().get("limit_range") or [1, 50])[:2]
    has_top_n = False
    limit = tree.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal):
        text = str(limit.expression.this)
        if text.isdigit() and int(low) <= int(text) <= int(high):
            has_top_n = True
        elif "zzslotxx" in text:
            # 挖槽时把白名单内的 LIMIT 换成了自己的占位符（`LIMIT 'zzslotxxtop_nxxzz'`）：
            # 那说明**这里本来就是"取前 N 名"**。不认这个占位符的话，"GROUP BY 类型
            # ORDER BY 数量 DESC LIMIT 占位"会被记成 distribution，而语义层对同一句话给的是
            # top → 模板永远匹配不上（docs/模板spec与现行语义漂移核查.md §3.1）。
            has_top_n = True
    has_order = tree.args.get("order") is not None
    if not group_by and has_order and has_top_n:
        # 不分组取极值（"面积最大的那条/置信度最高的那个"）：SQL 是 ORDER BY <列> + LIMIT n，
        # 计划侧给的指标就是那一列的极值（max_area / min_confidence…）。按结构推断，
        # 不看问题措辞——以前这里不登记指标，于是计划 metrics=[max_area] 永远对不上。
        order = tree.args.get("order")
        ordered = list(order.expressions) if order is not None else []
        if len(ordered) == 1:
            column = ordered[0].find(exp.Column)
            if isinstance(column, exp.Column):
                func = "max" if ordered[0].args.get("desc") else "min"
                metrics.append(_metric_name(func, str(column.name)))
    if group_by and has_order and has_top_n:
        shape = "top"
    elif bucket_group and not group_by:
        shape = "bucket_compare"
    elif group_by:
        # 时间维度的分组只有一种 SQL 结构：趋势题（trend）和"按月/按天分别多少"（distribution）
        # 是同一个结构，两个 shape 都要认，否则按趋势挖出来的模板永远匹配不上（P1-6）。
        shape = "trend" if set(group_by) & _TIME_GROUPS else "distribution"
    elif has_order and has_top_n:
        # 不分组取极值（"面积最大的那条"）：SQL 是 ORDER BY + LIMIT，不是明细列表
        shape = "top"
    elif metrics and any(item == "count" or item.startswith("distinct") for item in metrics):
        shape = "count"
    elif metrics and all(item.startswith("total_") for item in metrics):
        shape = "sum"
    elif metrics and all(item.startswith("avg_") for item in metrics):
        shape = "avg"
    elif metrics:
        # 聚合类（max_/min_ 这类）：原来写私有取值 "aggregate"，而语义层的 shape 词表
        # （nl2sql._SEMANTIC_SHAPES）里没有它 → 这条记录**永远匹配不上**，等于死模板。
        # 按语义层真正会给出的三个聚合 shape 记；拦住"只问 count 的问题"的是**指标覆盖闸**
        # （plan metrics ⊆ 模板 metrics），不是 shape
        # （docs/模板spec与现行语义漂移核查.md §3.4）。
        shape = "count"
        agg_shapes = ["count", "sum", "avg"]
    elif plain_columns:
        shape = "list"
    else:
        shape = ""
    # shapes：命中的 shape 白名单（不靠措辞猜）。分档只认 bucket_compare——
    # 它的 SQL 是"按分档 CASE 分组"，普通计数题套上来会用错口径。
    shapes = agg_shapes or ([shape] if shape else [])
    if shape == "trend":
        shapes = ["trend", "distribution"]
    return {
        "shape": shape,
        "shapes": shapes,
        "subjects": subjects[:2],
        "group_by": sorted(set(group_by)),
        "metrics": sorted(set(metrics)),
    }


def _schema_text(kind: str = "mysql", limit: int = 120) -> str:
    """给挖槽模型看的字段字典（表.列 + 注释 + 枚举），只列有业务的列。"""
    lines: list[str] = []
    for table in schema_catalog.field_catalog(kind).get("tables") or []:
        columns = []
        for col in table.get("columns") or []:
            if str(col.get("name")) in {"id", "del_flag", "create_time", "update_time", "update_by", "tenant_id"}:
                continue
            text = f"{col.get('name')}({col.get('label')})"
            code_map = col.get("code_map") or {}
            if code_map:
                text += "[" + ",".join(f"{code}={label}" for code, label in list(code_map.items())[:8]) + "]"
            columns.append(text)
        if not columns:
            continue
        lines.append(f"{table.get('table')}: " + "; ".join(columns[:limit]))
        if len(lines) >= 24:
            break
    return "\n".join(lines)


def _llm_skeleton(
    question: str, sql: str, kind: str = "mysql"
) -> tuple[dict[str, Any] | None, str]:
    """规则挖不动时，用便宜模型只做"哪个字面值属于哪一列"的归类，其它一律以目录为准。"""
    from . import prompts
    from .llm import LLMError, chat_json, role_model

    try:
        data = chat_json(
            prompts.messages(
                "templatize_sql",
                kind=kind,
                schema=_schema_text(kind),
                question=str(question or ""),
                sql=str(sql or ""),
            ),
            model=role_model("semantic"),
        )
    except LLMError as exc:
        log_error("templatize", "llm_fallback_failed", exc, question=str(question)[:60])
        return None, "模型挖槽不可用"
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "llm_fallback_failed", exc, question=str(question)[:60])
        return None, "模型挖槽异常"
    if not isinstance(data, dict) or not data.get("templatizable"):
        return None, f"模型判定不可模板化：{str((data or {}).get('reason') or '')[:40]}"
    param_sql = str(data.get("param_sql") or "")
    if not param_sql:
        return None, "模型没给出参数化 SQL"
    try:
        sqlglot.parse_one(_PLACEHOLDER.sub("1", param_sql), read="mysql")
    except Exception:  # noqa: BLE001
        return None, "模型给的参数化 SQL 解析失败"
    ctx = {"aliases": {}, "tables": set(), "index": _column_index(), "equivalents": {}, "seq": 0}
    params: dict[str, Any] = {}
    values: dict[str, str] = {}
    declares: dict[str, str] = {}
    for slot in data.get("slots") or []:
        name = str((slot or {}).get("name") or "").strip()
        column = str((slot or {}).get("column") or "").strip()
        if not name or not column or f"{{{name}}}" not in param_sql:
            continue
        info = _classify_plain(str((slot or {}).get("table") or ""), column, ctx)
        if info is None or info.get("type") in {"fixed", "bool_image"}:
            return None, f"模型给的条件按字段目录认不出来：{column}"
        extra: dict[str, Any] = {"type": info["type"], "options": info.get("options") or []}
        if info.get("type") == "time":
            # 时间槽必须是成对的起止；名字对不上就交回规则路径，别猜
            if "end" in name:
                name = "time_end"
            elif "start" in name:
                name = "time_start"
            else:
                return None, "时间条件的起止对不上"
            extra = {"type": "time", "source": "months"}
        if str(info.get("type")) == "numeric" and name.endswith("_op"):
            extra = {
                "type": "op",
                "options": [
                    {"label": item, "value": item} for item in (_cfg().get("numeric_ops") or [])
                ],
            }
        spec = _slot_spec(info, column, extra)
        param, declare = _name_for(str(info["type"]), info, column)
        if str(info["type"]) in {"entity", "enum"} and name != param:
            name = param
        params[name] = spec
        values[name] = str((slot or {}).get("original_value") or "")
        declares[name] = "time_range" if info.get("type") == "time" else declare
    if not params or not values:
        return None, "模型没给出可用的槽位"
    leftover = set(_PLACEHOLDER.findall(param_sql)) - set(params)
    if leftover:
        return None, "还有没归类的占位：" + "、".join(sorted(leftover))
    if any(not values[name] for name in params):
        return None, "模型没给原始值，回填验证不了"
    return (
        {
            "question": str(question or "").strip(),
            "sql": param_sql,
            "params": params,
            "values": values,
            "declares": declares,
            "fingerprint": fingerprint(param_sql),
            "kind": "mysql",
        },
        "",
    )


def _merge_cluster(items: list[dict[str, Any]], existing: dict[str, Any] | None) -> dict[str, Any] | None:
    base = dict(items[0])
    # 增量挖掘时 base 可能是**上一版记录里的样本**（脏问法），入记录前统一清一遍
    base["question"] = clean_template_question(
        str(base.get("question") or ""), str(base.get("sql") or "")
    )
    existing = existing or {}
    # 跨样本证据 = 本轮样本 + 上一版记录里存过的样本。
    # 增量挖掘时本轮往往只有**新加的那一条**，不带旧样本就会把"跨样本取过不同值"的证据丢掉：
    # 参数被误降级成 fixed、variants 也只剩新的那条（旧问法凭空消失）。
    prior_samples = [item for item in (existing.get("samples") or []) if isinstance(item, dict)]
    evidence: list[dict[str, Any]] = list(items) + [
        {"question": str(item.get("question") or ""), "values": dict(item.get("values") or {})}
        for item in prior_samples
    ]
    params_spec: dict[str, Any] = dict(base["params"])
    declares: dict[str, str] = dict(base.get("declares") or {})
    sql = str(base["sql"])
    fixed: dict[str, str] = {}
    # 挖不动、但原样留在 SQL 里的条件（"未识别"这类）→ 直接进 fixed/conditions
    fixed.update(dict(base.get("fixed_extra") or {}))
    confirmed: dict[str, Any] = {}
    for name, spec in params_spec.items():
        if "{" + name + "}" not in sql and "'" + "{" + name + "}" + "'" not in sql:
            continue
        values = [
            str(item["values"].get(name, ""))
            for item in evidence
            if name in (item["values"] or {})
        ]
        if not values:
            continue
        # 取值域自带"会变"证据的槽（路线/病害类型实体、时间、数值、TopN）由规则直接确认：
        # 一条"西乡段泛油有多少处"就该能复用成"恒明珠段坑槽有多少"（方案第九章验收标准）。
        # 编码类槽（上下行/收藏/工单状态/有无图片）的写死值可能是有意为之，
        # 必须由"跨样本取过不同值"来证实，否则降级为 fixed。
        rule_confirmed = str(spec.get("type")) in {"entity", "time", "numeric", "int", "op"}
        if len(set(values)) >= 2:
            confirmed[name] = spec
            spec["confirmed_by"] = "sample"
            continue
        if rule_confirmed:
            confirmed[name] = spec
            spec["confirmed_by"] = "domain"
            continue
        value = values[0]
        sql = _fill_fixed(sql, name, spec, value)
        declare = str(declares.get(name) or "")
        if declare:
            fixed[declare] = value
    for name, spec in params_spec.items():
        if name in confirmed:
            spec["declare"] = str(declares.get(name) or "")
    # 剩下的占位符必须都有对应的已确认槽（有占位却没人管 = 回填不了，直接丢掉）
    leftover = {name for name in _PLACEHOLDER.findall(sql)}
    if leftover - set(confirmed):
        return None
    conditions = [
        str(declares.get(name) or "")
        for name in params_spec
        if name in confirmed or str(declares.get(name) or "") in fixed
    ]
    conditions = [item for item in conditions if item]
    conditions += [item for item in fixed if item not in conditions]
    # mature = 有可替换的槽（能"换个路线/换种病害/换个阈值"复用）；
    # candidate = 所有位置都被证实是固定值（等于一条固化 SQL，交给示例库那条路走），
    # 或者**口径有风险**（计数口径可能漏算）——后者必须人工核对后手动转正。
    risk = str(base.get("risk") or "")
    status = "candidate" if risk else ("mature" if confirmed else "candidate")
    # 空表上的模板只能"结构验证"（回环跑出来 0 行 == 0 行，验不出口径）→ 不给 mature，
    # 先留在维护页待人工/待数据（docs/从打地鼠到回归防护体系方案.md §六.③）。
    # 人工转正（promoted_at）仍然压过这条：那是人点过的，不该被自动降级。
    review_reason = ""
    if status == "mature":
        empties = empty_source_tables(sql)
        if empties:
            status = "candidate"
            review_reason = "源表暂无数据，只能结构验证：" + "、".join(empties)
            log_event(
                "templatize",
                "INFO",
                "template_needs_review_empty_source",
                tables="、".join(empties),
                question=str(base.get("question") or "")[:50],
            )
    # 人工转正过的模板不在自动重挖里被降级：promoted_at 要继承下来，否则管理员点了"转正"，
    # 下一轮重挖又变回 candidate（docs/推荐模板链路Bug清单.md P1-7）。
    promoted_at = str((existing or {}).get("promoted_at") or "")
    if promoted_at and not risk:
        status = "mature"
        review_reason = ""
    # 自画像来自这条 SQL 的 AST（不是第一条问题原文），换参数复用才不会判错口径
    profile = dict(base.get("profile") or {})
    variants: list[str] = []
    for item in evidence:
        question = clean_template_question(str(item.get("question") or ""), sql)
        if question and question not in variants:
            variants.append(question)
    samples: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for item in list(items) + prior_samples:
        # 样本问法同样清一遍：界面/同义召回读的就是它们
        question = clean_template_question(str(item.get("question") or ""), sql)
        if question in seen_questions:
            continue
        seen_questions.add(question)
        samples.append({"question": question, "values": dict(item.get("values") or {})})
    record = {
        "id": f"auto_{base['fingerprint']}",
        "source": "auto",
        "title": str((existing or {}).get("title") or base["question"][:24] or "自动模板"),
        "question": base["question"],
        "variants": variants,
        "kind": "mysql",
        "dialect": "physical",
        "sql": sql,
        "params": confirmed,
        "match": {
            "shapes": [str(item) for item in (profile.get("shapes") or [])],
            "subjects": [str(item) for item in (profile.get("subjects") or [])],
            "group_by": [str(item) for item in (profile.get("group_by") or [])],
            "metrics": sorted({str(item) for item in (profile.get("metrics") or [])}),
            "conditions": sorted(set(conditions)),
            "fixed": fixed,
        },
        "fingerprint": base["fingerprint"],
        "status": status,
        # 给人看的"为什么不给 mature"（维护页可见）：目前只有"源表暂无数据"这一种
        "review_reason": review_reason,
        "promoted_at": promoted_at,
        "risk": risk,
        "trusted": True,
        "enabled": bool((existing or {}).get("enabled", True)),
        "usage": int((existing or {}).get("usage") or 0),
        "samples": samples,
        "created_at": str((existing or {}).get("created_at") or _now()),
    }
    return record


def _template_static_issues(sql: str) -> list[str]:
    """模板 SQL 的确定性硬错误：字段契约、表列不存在、编码值不在字典里。

    为什么要有这道闸：示例库里有一条问答 `结构类型为'上跨'的桥梁有多少座？`，它写死
    `bridges.crossing_type = '上跨'`，而这一列是**跨越地物类型**（1 河流/2 跨海/3 沟壑/
    4 管道/5 道路/6 铁路…），根本没有"上跨"这个取值——既不是同一维度，也不在 code_map 里。
    这样的模板永远查回 0 行，却会被当成确定性口径推荐（实测 auto_5e6b27171d79de80）。
    静态校验一直在报这条警告，但挖掘流程不看它；不在这儿拦，点一次"全量重挖"它就回来。
    """
    from . import static_check

    try:
        problems = static_check.check_sql(str(sql or ""), "mysql")
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "code_value_check_failed", exc)
        return []
    return [
        item
        for item in problems
        if str(item).startswith(
            (
                "表不在字段目录里",
                "列不存在",
                "编码值不在字典里",
                "CTE 字段契约不成立",
                "派生表字段契约不成立",
            )
        )
    ]


def _verify(record: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """回环验证（方案 §2.6）：结构一致 + 实跑结果一致。返回空串=通过。"""
    for item in items:
        sql, reason = bind(record, dict(item["values"]))
        if not sql:
            return f"回填失败：{reason}"
        try:
            if _structure_signature(sql) != _structure_signature(_original_sql(item)):
                return "回填后结构不一致"
        except Exception as exc:  # noqa: BLE001
            return f"回填后解析失败：{exc}"
        try:
            if not _same_rows(_rows_of(sql), _rows_of(_original_sql(item))):
                return "回填后结果不一致"
        except DatabaseError as exc:
            return f"回填后执行失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            return f"回填后执行异常：{exc}"
    return ""


def _original_sql(item: dict[str, Any]) -> str:
    return str(item.get("raw_sql") or "")


def _source_key(row: dict[str, Any]) -> str:
    """问答对的稳定身份（台账键）：优先文档名，其次"问题+SQL"的哈希。"""
    name = str(row.get("doc_name") or "").strip()
    if name:
        return name
    raw = f"{row.get('question') or ''}\n{row.get('sql_text') or ''}".encode("utf-8")
    return "src-" + hashlib.md5(raw).hexdigest()[:16]


def mine_all(force: bool = False, limit: int = 500, manual: bool = False) -> dict[str, Any]:
    """批量挖掘示例库里的问答对（后台/approve 后异步调用，不在问答请求里跑）。

    **不再筛 trusted**：凡是带 SQL 的已存示例都参与挖掘（它们本来就是人工存进示例库 /
    RAGFlow 的），能不能成熟仍由下面的回环验证说了算
    （docs/示例参数对齐与同义召回放宽方案.md 第二节）。

    增量（默认）：只处理"已挖掘台账"里没有的问答对——新加进 RAGFlow/示例库的那批会被尝试，
    已经挖过的（成了模板或明确挖不动）直接跳过；**已有模板原样保留**，不重算、不降级，
    `promoted_at` 也不会丢。翻遍全库重来用 force=True。
    manual=True：管理页按钮触发，不受"自动挖掘"开关限制（那个开关只管自动触发）。
    """
    from .. import store
    from . import chat_settings

    if (
        not force
        and not manual
        and not bool(chat_settings.load().get("auto_template_mine_enabled", False))
    ):
        # 挖掘开关默认关（docs/确认卡重复气泡耗时整改与污染模板库重置.md §三）：
        # 现有模板库被污染过，挖掘器修好之前不再自动产生新模板。
        log_event("templatize", "INFO", "mine_skipped_disabled")
        # 早退也要回**全字段**：前端直接读 pairs/mined/templates/mature，
        # 只回 skipped 会让管理页显示"可模板化 None/None 条"
        # （docs/推荐模板链路Bug清单.md P1-8）。
        return {
            "skipped": "auto_template_mine_enabled=false",
            "pairs": 0,
            "pending": 0,
            "mined": 0,
            "templates": 0,
            "mature": 0,
            "candidate": 0,
            "verified": 0,
            "rejected": {},
        }

    with _LOCK:
        rows = store.list_recorded_example_pairs(limit=limit)
        pairs = [row for row in rows if str(row.get("sql_text") or "").strip()]
        seen_pairs = {
            (str(row.get("question") or ""), str(row.get("sql_text") or ""))
            for row in pairs
        }
        # 独立复合题的每个子问题都可以单独挖模板。不能只依赖内层示例行：
        # 同型问法可能已去重到另一条记录，此时外层 subtasks_summary 仍保有原始 SQL。
        for row in rows:
            try:
                parent_spec = json.loads(str(row.get("spec_json") or "{}") or "{}")
            except Exception:  # noqa: BLE001
                continue
            subtasks = parent_spec.get("subtasks_summary") or []
            if parent_spec.get("query_type") != "composite" or not subtasks:
                continue
            independent = parent_spec.get("independent")
            if independent is None:
                independent = all(
                    not item.get("needs") and not item.get("produces") for item in subtasks
                )
            if not independent:
                continue
            parent_question = str(row.get("question") or "")
            for item in subtasks:
                question = str(item.get("question") or "")
                sql = str(item.get("sql") or "")
                if not question or not sql or (question, sql) in seen_pairs:
                    continue
                child_spec = dict(item.get("spec") or {})
                child_spec["composite_parent"] = parent_question
                child_spec["composite_independent"] = True
                pairs.append(
                    {
                        "question": question,
                        "sql_text": sql,
                        "doc_name": f"{row.get('doc_name') or _source_key(row)}::step:{item.get('id')}",
                        "trusted": row.get("trusted"),
                        "spec_json": json.dumps(child_spec, ensure_ascii=False),
                    }
                )
                seen_pairs.add((question, sql))
        # 已挖掘台账：force 时清空重来，否则只处理台账里没有的（新加进来的那批）
        ledger: dict[str, Any] = {} if force else dict(load().get("mined") or {})
        pending = [
            (key, row)
            for row in pairs
            if (key := _source_key(row)) not in ledger
        ]
        skipped = len(pairs) - len(pending)
        mined: list[dict[str, Any]] = []
        rejected: dict[str, int] = {}
        reject_rows: list[dict[str, Any]] = []
        from . import example_reuse

        for key, row in pending:
            # 多对象对比（≥2 条路线/桥梁/边坡/隧道/病害类型/月份）**不挖模板**：
            # 这类 SQL 常有多个 CTE/子查询，参数与别名对齐风险高、复用收益低
            # （docs/模板沉淀标准与复用校验_系统性方案.md §2.2-6）。这类题仍可走相似问法召回。
            try:
                row_spec = json.loads(str(row.get("spec_json") or "{}") or "{}")
            except Exception:  # noqa: BLE001
                row_spec = {}
            if row_spec.get("composite_parent") and not row_spec.get(
                "composite_independent", False
            ):
                reason = "依赖型复合子问题不沉淀模板（needs/produces 非空）"
                rejected[reason] = rejected.get(reason, 0) + 1
                reject_rows.append(
                    {"question": str(row.get("question") or "")[:60], "reason": reason}
                )
                log_event(
                    "templatize",
                    "INFO",
                    "rejected",
                    fingerprint="dependent-composite",
                    question=str(row.get("question") or "")[:40],
                )
                ledger[key] = {"template": "", "reason": reason, "at": _now()}
                continue
            if example_reuse.is_multi_object(
                row_spec, str(row.get("question") or ""), deep=True
            ):
                reason = "多对象对比不沉淀模板（收益低风险高）"
                rejected[reason] = rejected.get(reason, 0) + 1
                reject_rows.append(
                    {"question": str(row.get("question") or "")[:60], "reason": reason}
                )
                log_event(
                    "templatize",
                    "INFO",
                    "rejected",
                    fingerprint="multi-object",
                    question=str(row.get("question") or "")[:40],
                )
                ledger[key] = {"template": "", "reason": reason, "at": _now()}
                continue
            skeleton, reason = mine_sql(str(row.get("question") or ""), str(row.get("sql_text")), "mysql")
            if not skeleton:
                rejected[reason] = rejected.get(reason, 0) + 1
                reject_rows.append(
                    {"question": str(row.get("question") or "")[:60], "reason": reason}
                )
                ledger[key] = {"template": "", "reason": reason, "at": _now()}
                continue
            # 回环验证要用**能直接执行的那条 SQL**（Wren 的 MDL-SQL 得用编译出来的物理版）
            skeleton["raw_sql"] = str(skeleton.get("effective_sql") or row["sql_text"])
            skeleton["_source"] = key
            mined.append(skeleton)
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in mined:
            groups.setdefault(str(item["fingerprint"]), []).append(item)
        existing = {str(item.get("fingerprint")): item for item in list_templates()}
        # 增量时已有模板原样保留（本轮没动它们）；force 时按最新规则全部重算
        kept: list[dict[str, Any]] = [] if force else list(existing.values())
        fresh: list[dict[str, Any]] = []
        verified = 0
        for fp, items in groups.items():
            sources = [str(item.get("_source") or "") for item in items]
            record = _merge_cluster(items, existing.get(fp))
            if not record:
                rejected["占位没填完"] = rejected.get("占位没填完", 0) + 1
                for key in sources:
                    ledger[key] = {"template": "", "reason": "占位没填完", "at": _now()}
                continue
            # 完整性标记（方案 §4.3）：问题里有多个子问题、而这条 SQL 只覆盖一部分时，
            # 卡片上要如实说明"这只回答了其中一部分"，不能让用户以为全答了。
            record["partial"] = question_is_partial(
                str(row.get("question") or record.get("question") or ""),
                [
                    str(item)
                    for item in ((record.get("match") or {}).get("metrics") or [])
                ],
            )
            # 字段契约或编码值不成立 → 这条模板执行必错/永远查不到数据，不许沉淀。
            static_issues = _template_static_issues(str(record.get("sql") or ""))
            if static_issues:
                rejected["静态字段契约不通过"] = rejected.get("静态字段契约不通过", 0) + 1
                reject_rows.append(
                    {
                        "question": str(record.get("question") or "")[:60],
                        "reason": "静态字段契约不通过：" + "；".join(static_issues[:2]),
                    }
                )
                log_event(
                    "templatize",
                    "WARNING",
                    "rejected",
                    fingerprint=fp,
                    reason=static_issues[0][:80],
                )
                for key in sources:
                    ledger[key] = {
                        "template": "",
                        "reason": "静态字段契约不通过：" + static_issues[0][:60],
                        "at": _now(),
                    }
                continue
            reason = _verify(record, items)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                log_event("templatize", "WARNING", "rejected", fingerprint=fp, reason=reason)
                for key in sources:
                    ledger[key] = {"template": "", "reason": reason, "at": _now()}
                continue
            verified += 1
            fresh.append(record)
            for key in sources:
                if key:
                    ledger[key] = {"template": str(record.get("id")), "at": _now()}
        by_fingerprint = {str(item.get("fingerprint")): item for item in kept}
        for record in fresh:
            by_fingerprint[str(record.get("fingerprint"))] = record
        templates = list(by_fingerprint.values())
        # 只有 mature 才有资格进对话推荐；candidate 只在维护页待确认区累积
        templates.sort(key=lambda item: (item["status"] != "mature", -len(item.get("variants") or [])))
        _write_rejects(reject_rows)
        _write(templates, mined=ledger)
        stats = {
            "pairs": len(pairs),
            "pending": len(pending),
            "skipped": skipped,
            "mined": len(mined),
            "templates": len(templates),
            "fresh": len(fresh),
            "mature": sum(1 for item in templates if item["status"] == "mature"),
            "candidate": sum(1 for item in templates if item["status"] == "candidate"),
            "verified": verified,
            "rejected": rejected,
            "force": bool(force),
        }
        log_event("templatize", "INFO", "mine_done", **{k: v for k, v in stats.items() if k != "rejected"})
        # 挖完顺手做一次生命周期巡检（broken / cold）：都在后台线程里跑，不占请求时间
        try:
            stats["sweep"] = sweep_templates()
        except Exception as exc:  # noqa: BLE001
            log_error("templatize", "sweep_failed", exc)
        # 新沉淀的模板补一次 LLM 审核（限量；只在后台挖矿线程里跑，不占对话时间）
        try:
            stats["review"] = review_pending(limit=2)
        except Exception as exc:  # noqa: BLE001
            log_error("templatize", "review_pending_failed", exc)
        return stats


def mine_in_background(name: str = "templatize") -> None:
    """后台跑一次**增量**挖掘：approve / 示例同步之后调用，不占请求时间。

    失败只记日志；新模板落库后快捷查询下一次拉取就能看到（注册表是按 mtime 热加载的）。
    """
    import threading

    def _run() -> None:
        try:
            mine_all()
        except Exception as exc:  # noqa: BLE001
            log_error("templatize", "background_mine_failed", exc)

    threading.Thread(target=_run, name=name, daemon=True).start()


# ── 运行期：匹配 / 绑定 / 卡片槽位 ──────────────────────────────────────────
_ALIAS_CONDITIONS = {"route_name": "route", "problem_type_name": "damage_type", "damage_type_name": "damage_type"}

# ── 条件键规范化：挖掘侧（declares / match.conditions / match.fixed）和计划侧
#    （required_conditions / semantic_conditions.field / 结构化槽）各自起名，直接做集合判定会把
#    同一个条件当成两个（`num_area` vs `area`、`route_id` vs `route`），只能靠特例去凑。
#    这里统一翻成一套规范名，整条匹配链路都不再看措辞/关键词
#    （docs/推荐模板链路Bug清单.md P1-6）。
_CONDITION_KEYS = {
    **_ALIAS_CONDITIONS,
    "route_id": "route",
    "routes_id": "route",
    "problem_type": "damage_type",
    "damage_matches": "damage_type",
    "time_start": "time_range",
    "time_end": "time_range",
    "collection_time": "time_range",
    "month": "time_range",
    "day": "time_range",
    "year": "time_range",
    "week": "time_range",
    "hour": "time_range",
    "problem_url": "has_image",
    "preview_url": "has_image",
    "image": "has_image",
}

# 时间维度的分组键（趋势题与"按月/按天分布"题共用同一个 SQL 结构）
_TIME_GROUPS = {"month", "day", "year", "week", "dow", "hour"}


def condition_key(name: str) -> str:
    """条件名 → 规范键（数值阈值槽 `num_area` 与计划里的列条件 `area` 是同一个条件）。"""
    text = str(name or "").strip().lower()
    if not text:
        return ""
    if text.startswith("num_"):
        return text[4:]
    return _CONDITION_KEYS.get(text, text)


def plan_condition_value(plan: dict[str, Any], key: str) -> str | None:
    """计划里这个规范条件对应的值；**问题没给这个条件时返回 None**。"""
    slots = plan.get("slots") or {}
    if key == "route":
        value = str((slots.get("route_match") or {}).get("id") or "").strip()
        return value or None
    if key == "damage_type":
        hits = {
            str(item.get(field) or "").strip()
            for item in slots.get("damage_matches") or []
            for field in ("problem_type", "problem_code")
        }
        hits = {item for item in hits if item}
        return ",".join(sorted(hits)) if hits else None
    if key == "time_range":
        start, end = slots.get("time_start"), slots.get("time_end")
        return f"{start or ''},{end or ''}" if (start or end) else None
    if key == "pile_range":
        low, high = slots.get("pile_start"), slots.get("pile_end")
        return f"{low},{high}" if (low is not None or high is not None) else None
    for item in slots.get("semantic_conditions") or []:
        if condition_key(item.get("field")) == key:
            value = item.get("value")
            return "" if value is None else str(value)
    value = slots.get(key)
    if value in (None, "", [], {}):
        return None
    return str(value)


def plan_condition_keys(plan: dict[str, Any]) -> set[str]:
    """这条问题明确给出的过滤条件（规范键集合）。"""
    keys = {condition_key(item) for item in plan.get("required_conditions") or []}
    keys |= {
        condition_key(item.get("field"))
        for item in (plan.get("slots") or {}).get("semantic_conditions") or []
    }
    for key in ("route", "damage_type", "time_range", "pile_range", "up_or_down", "has_image"):
        if plan_condition_value(plan, key) is not None:
            keys.add(key)
    # 桩号条件只有一个：语义层在 `pile_distance` 列上抽的区间/等值条件（LLM 给的
    # `semantic_conditions`），和规则按 `slots.pile_start/pile_end` 加的 `pile_range` 说的是
    # **同一件事**（"K0+800 到 K0+900"两边都会出现）。同时报两个键会让模板"表达不了这个条件"
    # 而被弃权——实测 4 条 auto 模板就卡在这里（docs/模板spec与现行语义漂移核查.md §3.2）。
    # 存下来的模板条件键用的是 `pile_distance`（`num_pile_distance` 归一后的名字），所以留它。
    if {"pile_distance", "pile_range"} <= keys:
        keys.discard("pile_range")
    return {item for item in keys if item}


def condition_agrees(plan_value: str, fixed_value: str) -> bool:
    """计划里的值是否与模板写死的值一致（多值按"任一命中"判定）。"""
    target = str(fixed_value or "").strip().lower()
    if not target:
        return True
    parts = {item.strip().lower() for item in str(plan_value or "").split(",") if item.strip()}
    return target in parts


# 槽位声明的语义角色 → 实体表（外键关系不在目录里时，用它兜底现查取值域）
# 覆盖用户要求的主体：disease/pile_point/section/patrol/work_order/asset/route（+桥隧坡/工程）
_DECLARE_TABLES: dict[str, str] = {
    "route": "basic_facilities_routes",
    "section": "basic_facilities_sections",
    "pile_point": "basic_facilities_sections_pile_detail",
    "patrol": "ai_maintain_patrol",
    "work_order": "ai_work_order",
    "asset": "ai_asset_base",
    "bridge": "basic_facilities_bridges",
    "tunnel": "basic_facilities_tunnels",
    "slope": "basic_facilities_slope",
    "project": "project_manage_project_main",
}
# 实体表的显示列优先级（取值域下拉里给人看的那一列）
_DISPLAY_COLUMN_CANDIDATES = (
    "short_name",
    "full_name",
    "name",
    "pile_map_position_name",
    "pile_map_position",
    "problem_type",
    "order_name",
    "asset_type",
    "project_name",
    "code",
)


def _display_column_of(table: str) -> str:
    """这张实体表用哪一列当"给人看的名字"（按候选顺序取第一个真实存在的列）。"""
    try:
        entry = schema_catalog.table_entry("mysql", table) or {}
    except Exception:  # noqa: BLE001
        return ""
    names = {str(item.get("name")) for item in entry.get("columns") or []}
    for candidate in _DISPLAY_COLUMN_CANDIDATES:
        if candidate in names:
            return candidate
    return ""


def refresh_options(spec: dict[str, Any]) -> list[dict[str, str]]:
    """**连库现查**这个槽当前的取值域（模板里存的 options 是沉淀那一刻的快照）。

    为什么需要（docs/模板沉淀标准与复用校验_系统性方案.md §4.1 的延伸）：模板的 entity 槽
    在沉淀时把路线/病害类型/桥隧坡等取值域**存成了快照**。库里后来新增了路线，用户问"新路线
    泛油有多少处"时 `coerce()` 在旧快照里找不到 → 路线槽（必填）拿不到值 → 整张卡被拒 →
    退回 Wren（慢）。老快照不该成为"新对象永远用不了"的原因。

    返回 [] 表示这类槽现查不了（时间/名次/取值域写死在配置里），调用方按原逻辑处理。
    """
    table = str(spec.get("table") or "")
    column = str(spec.get("column") or "")
    if not table or not column:
        return []
    # ① 源列自己带的外键（最常见：`ai_problem.routes_id` → routes 的 id/ short_name）
    try:
        from . import schema_catalog

        entry = schema_catalog.table_entry("mysql", table) or {}
        col = next(
            (item for item in entry.get("columns") or [] if str(item.get("name")) == column),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "refresh_options_failed", exc, table=table, column=column)
        col = None
    join = (col or {}).get("join")
    if isinstance(join, dict) and join.get("table"):
        return _entity_options(
            str(join["table"]), str(join.get("on") or ""), str(join.get("column") or "")
        )
    # ② MDL 逻辑展示列（`problem_type_name` 这类）走"显示列 → 目标表"索引
    display = _fk_display_targets().get((table, column))
    if display:
        return _entity_options(
            str(display.get("table")), str(display.get("value_column")), str(display.get("label_column"))
        )
    # ③ 谓词直接比实体主键（`r.id IN (…)`）时，spec 里的 table 就是目标表
    target_ref = _fk_target_columns().get((table, column))
    if target_ref:
        return _entity_options(table, column, str(target_ref.get("display_column") or ""))
    # ④ 枚举列：取值域来自列注释的 code_map（每次现读字段目录，跟着库结构走）
    code_map = (col or {}).get("code_map") or {}
    if code_map:
        return [{"label": str(value), "value": str(key)} for key, value in code_map.items()]
    # ④.5 **取值域就是这一列自己**的槽：字典表/台账的"名称类"列（`ai_problem_base.problem_type`
    #    病害类型、`ai_asset_base.asset_type`…）。这类槽的 options 同样是沉淀时的快照——
    #    字典里新增一种病害类型，老模板就认不出来了（用户实测：参数槽几乎每一列都有 options）。
    #    直接 DISTINCT 这一列现查（和 `_entity_options` 里 value==label 的那条分支同一套）。
    if col is not None:
        live = _entity_options(table, column, column)
        if live:
            return live
    # ⑤ 兜底：槽位的 declare 说了它是哪一类实体（route/patrol/section/asset/work_order/
    #    桥隧坡…）→ 直接按"实体表 + 显示列"现查。目录里没有外键关系时也能用，
    #    覆盖范围就是主体清单本身（用户要求：options 过期要在**所有主体**上都能现查）。
    declare = str(spec.get("declare") or "").strip()
    target = _DECLARE_TABLES.get(declare, "")
    if target:
        display = _display_column_of(target)
        value_column = "id" if display else ""
        options = _entity_options(target, value_column or "id", display or "id")
        if options:
            return options
    return []


def _options(spec: dict[str, Any]) -> list[dict[str, str]]:
    if str(spec.get("type")) in {"entity", "enum"}:
        fresh = refresh_options(spec)
        if fresh:
            return fresh
    options = spec.get("options")
    if options:
        return [dict(item) for item in options]
    if str(spec.get("type")) == "time":
        return preset_lib.options_for("month", {"source": "months"})
    if str(spec.get("type")) == "int":
        low, high = (spec.get("range") or [1, 50])[:2]
        return [{"label": f"前 {i}", "value": str(i)} for i in range(int(low), int(high) + 1)]
    return []


def coerce(spec: dict[str, Any], raw: str) -> str | None:
    """把计划里/用户给的值翻成模板能用的值（中文标签 → code）。拿不准返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if str(spec.get("type")) == "time":
        # 精确到分钟的时间（plan 的 time_start/time_end 就是这种）直接用，
        # 只有"月份"这种粗粒度才退化成整月（P1-5：别把"7月30日"放大成"7月"）。
        if _DATETIME.match(text):
            return text
        if len(text) >= 7 and text[:4].isdigit() and text[5:7].isdigit():
            return text[:7]
        return None
    options = _options(spec)
    allowed = {str(item.get("value")): str(item.get("label")) for item in options}
    if allowed:
        form = str(spec.get("form") or "value")
        single = _coerce_one(text, allowed, form)
        # 多值槽（`r.id IN ('id1','id2')`）：按逗号拆开逐个翻，任一翻不出来就算拿不准
        if "," in text and spec.get("multi"):
            parts = [part.strip() for part in text.split(",") if part.strip()]
            mapped = [_coerce_one(part, allowed, form) for part in parts]
            if len(mapped) > 1 and all(item for item in mapped):
                return ",".join(str(item) for item in mapped)
        if single:
            return single
        # 快照里没找到 → 可能库里新增了对象（新路线/新桥/新工单类型…）：**连库现查一次**
        # 再映射（_entity_options 自带 TTL 缓存，不会每次都连库）。老快照因此不再是
        # "新对象永远复用不了"的枷锁（用户实测：新增路线后模板一直退回 Wren）。
        fresh = refresh_options(spec)
        fresh_allowed = {
            str(item.get("value")): str(item.get("label")) for item in fresh
        }
        if fresh_allowed and fresh_allowed != allowed:
            if "," in text and spec.get("multi"):
                parts = [part.strip() for part in text.split(",") if part.strip()]
                mapped = [_coerce_one(part, fresh_allowed, form) for part in parts]
                if len(mapped) > 1 and all(item for item in mapped):
                    log_event(
                        "templatize",
                        "INFO",
                        "options_refreshed",
                        table=str(spec.get("table")),
                        column=str(spec.get("column")),
                        value=text[:40],
                    )
                    return ",".join(str(item) for item in mapped)
            single = _coerce_one(text, fresh_allowed, form)
            if single:
                log_event(
                    "templatize",
                    "INFO",
                    "options_refreshed",
                    table=str(spec.get("table")),
                    column=str(spec.get("column")),
                    value=text[:40],
                )
                return single
        return None
    if str(spec.get("type")) in {"numeric", "op", "int"} and not _NUMBER.match(text):
        return None
    return text


def _coerce_one(text: str, allowed: dict[str, str], form: str) -> str | None:
    """单个取值 → 模板里该写的值（`form=label` 写中文名，否则写 id/code）。"""
    if text in allowed:
        return allowed[text] if form == "label" else text
    for value, label in allowed.items():
        if label == text or label.startswith(text):
            return label if form == "label" else value
    return None


def _plan_param_value(
    plan: dict[str, Any], name: str, spec: dict[str, Any], params: dict[str, Any] | None = None
) -> str | None:
    slots = plan.get("slots") or {}
    column = str(spec.get("column") or "")
    values = preset_lib.plan_slot_values(plan)
    conditions = list(slots.get("semantic_conditions") or [])
    if str(spec.get("type")) == "op" and str(name).endswith("_op"):
        # 比较符槽（`num_area_op`）要找**同一个数值列**的条件：先看同名的数值槽声明的列，
        # 没有再退回本槽的列。这里以前写成 str(...).get(...)，只要有阈值槽就会 AttributeError，
        # 异常被 capabilities 吞掉后整类自动模板候选都会消失。
        sibling = (params or {}).get(str(name)[:-3]) or {}
        base = str(sibling.get("column") or column)
        for item in conditions:
            if str(item.get("field") or "") == str(base or column):
                return str(item.get("op") or "")
        return None
    if name == "route_id":
        return values.get("route_id")
    if name == "damage_type":
        for item in slots.get("damage_matches") or []:
            if item.get("problem_type"):
                return str(item["problem_type"])
        return None
    if str(spec.get("type")) == "time":
        # 优先用 plan 里精确的起止时间（"7月30日"要保留到日），没有再退回月份
        exact = slots.get("time_start") if name.endswith("start") else slots.get("time_end")
        if exact:
            return str(exact)
        if values.get("month"):
            return str(values["month"])
        start = str(slots.get("time_start") or "")
        if len(start) >= 7:
            return start[:7]
        return None
    if name == "top_n":
        return values.get("top_n")
    if str(spec.get("declare") or "") == "bucket":
        # 分档粒度：语义层给的 `bucket_meters`（"每 500 米""每 1 公里"都由它理解，代码不认措辞）。
        # 用户没说粒度 → None（模板里带的默认值会显示在卡上，由用户决定要不要改）。
        granularity = slots.get("bucket_meters")
        if granularity in (None, ""):
            return None
        try:
            return str(int(float(granularity)))
        except (TypeError, ValueError):
            return None
    if str(spec.get("declare") or "") == "time_bucket":
        # 时间粒度：语义层判的分组维度（group_by=week/month/day…）就是粒度，
        # 在这儿翻成格式串填进槽（封闭词表，不猜措辞）。
        return _TIME_BUCKET_BY_GROUP.get(str(slots.get("group_by") or ""))
    if column and name.startswith("num_"):
        # 桩号区间（`num_pile_distance_low/high`）：值在**结构化槽位**里（pile_start/pile_end），
        # 不在语义条件里，所以要单独取一次，否则"K0+800~K0+900"这类模板在卡上永远缺参数。
        from . import nl2sql

        if str(column) in set(nl2sql._FIELD_FAMILIES.get("pile_number") or ()):
            pile_low = slots.get("pile_start")
            pile_high = slots.get("pile_end")
            picked = pile_high if str(name).endswith("_high") else pile_low
            if picked is not None:
                return str(int(picked))
        for item in conditions:
            if str(item.get("field") or "") == column or str(item.get("field") or "") == name:
                return str(item.get("value") or "")
        return None
    if column == "up_or_down" and slots.get("up_or_down"):
        return str(slots["up_or_down"])
    for item in conditions:
        field = str(item.get("field") or "")
        if field == name or field == column or _ALIAS_CONDITIONS.get(field) == name:
            return str(item.get("value") or "")
    return None


def _wants_top_n(plan: dict[str, Any]) -> bool:
    """"取前 N 名 / 最多几个"这类题型（shape=top，或语义层给了 limit）。"""
    if str(plan.get("shape") or "") == "top":
        return True
    return bool((plan.get("semantic") or {}).get("limit"))


def _plan_allows_template(plan: dict[str, Any]) -> bool:
    """计划层面的准入（与具体模板无关）。"""
    if str(plan.get("kind") or "mysql") != "mysql":
        return False
    if plan.get("comparison") and not plan.get("multi_route"):
        # 多路线题（comparison + multi_route）不在这里一刀切：能不能接由条件覆盖门槛决定
        # （只有自带多值实体槽的模板才满足）。
        return False
    return True


def card_for_template(
    template: dict[str, Any],
    plan: dict[str, Any],
    *,
    reason_prefix: str = "",
    score_bonus: float = 0.0,
) -> dict[str, Any] | None:
    """这条模板能不能接这条问题、接上之后卡片长什么样（只判定，不执行 SQL）。

    结构匹配（`match`）和 capabilities 的"同义问法召回"都走这里：条件覆盖、固定值冲突、
    缺槽弹性、卡片与 SQL 预览只有一份实现，两条入口的口径判定不会各自跑偏。
    reason_prefix / score_bonus 让调用方说清"这条是靠什么召回的"（结构命中 or 同义问法），
    相关性排序也据此加权。
    """
    if not _plan_allows_template(plan):
        return None
    if not template.get("enabled", True) or template.get("status") != "mature":
        return None
    shape = str(plan.get("shape") or "")
    subject = str(plan.get("subject") or "disease")
    group_by = str((plan.get("slots") or {}).get("group_by") or "")
    metrics = {str(item) for item in ((plan.get("semantic") or {}).get("metrics") or [])}
    structural = {"comparison"} if group_by else set()
    # 问题这边的条件全部翻成规范键，和模板声明做同一套判定（不看措辞、不看关键词）
    plan_keys = plan_condition_keys(plan)
    spec = template.get("match") or {}
    conditions = {str(item) for item in spec.get("conditions") or []}
    fixed = {str(k): str(v) for k, v in (spec.get("fixed") or {}).items()}
    covered = {condition_key(item) for item in conditions}
    covered |= {condition_key(item) for item in fixed}
    covered |= structural
    if "multi_route" in covered:
        # 多值实体槽自己就表达了"多个对象"（`r.id IN ('id1','id2')`）：
        # 它同时满足 required 里的 multi_route、comparison 和"路线条件"本身
        covered |= {"multi_route", "comparison", "route"}
    # 冷门：模板能表达的条件必须覆盖问题**全部**条件（宁可不匹配，不可错匹配）。
    # 以前这里分开比"required"和"semantic_fields"，且两边键名不一致
    # （num_area vs area）→ 数值阈值类条件永远对不上（docs/推荐模板链路Bug清单.md P1-6）。
    if not plan_keys <= covered:
        return None
    # `top_n`（取前 N 名）是**结构条件**：问题没问"前 N / 最多几个"，就不能拿一条带
    # LIMIT 的模板来套（实测"全库病害有多少种"被"桩号去重前 N"模板套上，
    # 摆出一张只能"待补 top_n"的空卡）。
    if "top_n" in covered and not _wants_top_n(plan):
        return None
    shapes_allowed = [str(item) for item in spec.get("shapes") or []]
    if shapes_allowed and shape not in shapes_allowed:
        # 老记录里可能留着挖掘器的私有形状 "aggregate"（语义层的 shape 词表里没有这个取值，
        # 永远不会给出）→ 那条模板本来是死的。按聚合类认
        # （docs/模板spec与现行语义漂移核查.md §3.4）。
        if not ("aggregate" in shapes_allowed and shape in {"count", "sum", "avg"}):
            return None
    if spec.get("subjects") and subject not in list(spec["subjects"]):
        return None
    if spec.get("group_by"):
        if group_by not in list(spec["group_by"]):
            return None
    elif group_by:
        return None
    if spec.get("metrics") and not metrics.issubset({str(item) for item in spec["metrics"]}):
        return None
    # 问题侧**指标完全没解析出来**（metrics 与 output_fields 都空）时，不许拿"带具体指标"的模板
    # 硬套：空集是任何集合的子集，光看那条会让"几车道"这种路线模板把"纬度范围"接走
    # （docs/推荐卡硬推荐与指标空匹配方案.md §三.2/§四.1）。这种题该交给同义召回/通用算子。
    plan_fields = {str(item) for item in ((plan.get("semantic") or {}).get("output_fields") or [])}
    if not metrics and not plan_fields and spec.get("metrics"):
        log_event(
            "templatize",
            "INFO",
            "template_skipped_no_metric",
            template=str(template.get("id")),
            question=str(plan.get("message") or "")[:60],
        )
        return None
    # 问题点名了具体字段（"纬度范围"→ lat）→ 模板里必须有那一列，**槽位齐不齐都要查**。
    # 下面"槽位齐、SQL 拼出来了"那条路已经有同样的检查，但缺槽的**弱候选**会绕过它：实测
    # 「西乡段的纬度范围」在语义层把主体判成 route 的那几轮（5 次里 3 次）被
    # "宝安大道西乡段有多少处泛油"这种缺 damage_type 的弱卡顶到第一位
    # （docs/推荐卡硬推荐与指标空匹配方案.md §四.3 的同一条规则，只是原来漏了缺槽那条路）。
    if plan_fields:
        from . import nl2sql as _nl2sql

        if not _nl2sql.sql_covers_fields(str(template.get("sql") or ""), sorted(plan_fields)):
            log_event(
                "templatize",
                "INFO",
                "template_skipped_field_missing",
                template=str(template.get("id")),
                fields="、".join(sorted(plan_fields))[:60],
                question=str(plan.get("message") or "")[:60],
            )
            return None
    values: dict[str, str] = {}
    missing: list[str] = []
    dropped: list[str] = []
    params = template.get("params") or {}
    for name, param_spec in params.items():
        raw = _plan_param_value(plan, name, param_spec, params)
        value = coerce(param_spec, raw or "")
        if value:
            values[name] = value
            continue
        # 缺槽不要一票否决：只有"会导致查错对象/错口径"的槽才硬淘汰
        # （路线；以及问题明确给了、模板却取不到值的那个条件），其余槽留给用户在卡上补
        # （docs/复杂模板匹配与推荐改造方案.md P0-3）。
        declare = str(param_spec.get("declare") or "")
        if name == "route_id" or (declare and condition_key(declare) in plan_keys):
            return None
        if is_optional_param(name, param_spec):
            # 可选条件本轮没提 → 不摆"待补"，bind 会把这段 WHERE 整段去掉再查（§4.2）
            dropped.append(name)
            continue
        missing.append(name)
    # 槽位完成度 = 0（所有槽都没从本轮计划取到值）= 这条模板跟问题只有结构像、内容一点没对上
    # （实测"全库有没有面积=5000 的异常记录"会撞上"列出每种病害类型对应的病害代码"）。
    # 直接淘汰，不是"留着让用户补"——零过滤的纯字典/明细题条件集为空，空集是任何集合的子集，
    # 不挡的话任何 list+disease 模板都能套进来（docs/auto模板误匹配与看板同义召回被关方案.md §二.2）。
    # 注意：**被丢掉的可选条件也要算进来**。否则"4 个槽一个都没从本轮问题取到"的模板
    # （实测 "k1+802 附近还有其他高置信度的病害记录吗？" 被"每种病害类型对应的病害代码"
    # 引出来）会因为"丢掉 3 个、只缺 1 个结构槽"而绕过这道闸，顶到通用算子前面。
    if params and len(missing) + len(dropped) >= len(params):
        log_event(
            "templatize",
            "INFO",
            "template_all_slots_missing",
            template=str(template.get("id")),
            question=str(plan.get("message") or "")[:60],
        )
        return None
    # 模板里写死的业务条件必须和**问题给的条件**一致（上行/下行、有图/无图…）。
    # 以前这段是遍历 params 找同名 declare，而被降级成 fixed 的槽恰恰不在 params 里，
    # 等于压根没校验：问"下行"也会推荐写死"上行"的模板（docs/推荐模板链路Bug清单.md P0-1）。
    # 现在直接对计划取值：问题没提这个条件 → 不命中（绝不替用户加条件）。
    for raw_condition, want in fixed.items():
        got = plan_condition_value(plan, condition_key(raw_condition))
        if got is None or not condition_agrees(got, want):
            return None
    # 槽位齐全时把 SQL 直接拼好（卡上预览的就是它）；缺槽时先摆"待补"让用户选
    sql = ""
    if not missing:
        sql, reason = bind(template, values)
        if not sql:
            return None
        # 问题点名了具体字段（"纬度范围"→ lat），模板 SQL 里却没有这一列 → 不许硬套
        # （docs/推荐卡硬推荐与指标空匹配方案.md §三.3）。
        plan_fields = [
            str(item) for item in ((plan.get("semantic") or {}).get("output_fields") or [])
        ]
        if plan_fields:
            from . import nl2sql as _nl2sql

            if not _nl2sql.sql_covers_fields(sql, plan_fields):
                log_event(
                    "templatize",
                    "INFO",
                    "template_field_mismatch",
                    template=str(template.get("id")),
                    fields="、".join(plan_fields)[:60],
                )
                return None
    filled = len(params) - len(missing)
    total = max(len(params), 1)
    # 被丢掉的可选条件算"半填"：卡片能执行了（比"待补"强），但命中范围比模板问法宽，
    # 不该和"条件全对上"拿一样的分。
    completeness = max(filled - 0.5 * len(dropped), 0.0) / total
    # 候选按契合度排序（不再所有模板一个分数）：槽位越全越靠前，用过的模板也加分
    score = (
        settings.auto_template_score_base
        + settings.auto_template_score_completeness * completeness
        - settings.auto_template_score_missing_penalty * len(missing)
        - settings.auto_template_score_missing_penalty * 0.5 * len(dropped)
    )
    score += min(int(template.get("usage") or 0), 10) / 100
    score += float(score_bonus or 0)
    reasons = [reason_prefix or "结构指纹 + 槽位覆盖匹配（0 次大模型）"]
    if missing:
        reasons.append("还差 %d 个条件待补：" % len(missing))
    if dropped:
        # time_start / time_end 标签同名，去重后再提示（"病害采集时间、病害采集时间"很难看）
        seen_labels: list[str] = []
        for name in dropped:
            label = str((params.get(name) or {}).get("label") or name)
            if label not in seen_labels:
                seen_labels.append(label)
        labels = "、".join(seen_labels)
        reasons.append(f"本轮没提的条件已按“不限”处理（{labels}），命中范围会比模板问法更宽")
    if template.get("usage"):
        reasons.append(f"已复用 {template.get('usage')} 次")
    # 完整性提示（方案 §4.3）：这条模板的问法本身有多个子问题，而它只覆盖一部分——
    # 明确告诉用户"其余部分要追问/让 AI 查"，不能让用户以为问到的都答了。
    partial = bool(template.get("partial")) and question_is_partial(str(plan.get("message") or ""))
    if partial:
        reasons.append("这条模板只覆盖原问题的一部分（其余部分需追问或让 AI 用 Wren 查）")
    return {
        "kind": "auto",
        "id": str(template.get("id")),
        "title": str(template.get("title") or template.get("id")),
        "question": str(template.get("question") or ""),
        "scope": "自动沉淀的模板："
        + str(template.get("question") or "")[:30]
        + "（0 次大模型）"
        + ("【只回答一部分】" if partial else ""),
        "dropped_conditions": dropped,
        "shape": shape,
        "subject": subject,
        "slots": card_slots(template, values, missing),
        "score": round(score, 2),
        "reasons": reasons,
        "warn": bool(missing) or partial or bool(dropped),
        "partial": partial,
        "sql_preview": sql,
        "missing_slots": missing,
        "template_id": str(template.get("id")),
    }


def match(plan: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """按语义 plan 结构匹配 auto 模板（只找候选，绝不执行）。"""
    if not _plan_allows_template(plan):
        return []
    out: list[dict[str, Any]] = []
    for template in list_templates():
        card = card_for_template(template, plan)
        if card:
            out.append(card)
    out.sort(key=lambda item: -float(item.get("score") or 0))
    return out[:limit]


# 可选条件：用户这轮没提到就"整段去掉再查"（docs/模板沉淀标准与复用校验_系统性方案.md §4.2）。
# 只有数据类条件可选；op（比较符）/ int（TopN）是**结构**参数，缺了整个口径就不成立。
_OPTIONAL_PARAM_KINDS = {"entity", "enum", "time", "numeric"}
_OPTIONAL_SENTINEL = "__CODEX_OPT_MISSING__"
_ALL_VALUE = "__ALL__"
_DATE_GRANULARITY_OPTIONS = [
    {"label": "按日", "value": "day"},
    {"label": "按周", "value": "week"},
    {"label": "按月", "value": "month"},
    {"label": "按季度", "value": "quarter"},
    {"label": "按年", "value": "year"},
]


# 路线**不做可选**：它是"数哪一摊"的落点，去掉等于换主体（"西乡段泛油有多少处"→"全库泛油"），
# 卡片标题会误导。时间/病害类型/桩号/阈值是**过滤**，去掉就是用户没问的那个维度（§4.2）。
_REQUIRED_PARAM_NAMES = {"route_id", "route_ids", "route_name"}


def is_optional_param(name: str, spec: dict[str, Any] | None) -> bool:
    param_name = str(name or "")
    if param_name in _REQUIRED_PARAM_NAMES or param_name.startswith("route"):
        return False
    return str((spec or {}).get("type")) in _OPTIONAL_PARAM_KINDS


def dropped_optional_names(
    template: dict[str, Any], values: dict[str, str] | None
) -> list[str]:
    """本轮没给值、会被 bind 整段丢掉的可选条件（卡片上要如实告诉用户）。"""
    out: list[str] = []
    for name, spec in (template.get("params") or {}).items():
        if not is_optional_param(name, spec):
            continue
        if not str((values or {}).get(name) or "").strip():
            out.append(name)
    return out


def _split_and(node: Any) -> list[Any]:
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def drop_conditions(sql: str, names: list[str]) -> str | None:
    """把"这些参数本轮没值"所在的 WHERE 分句**整段去掉**（方案 §4.2）。

    为什么不能只把值留空：`AND p.collection_time >= '' AND ...` 会把结果查成 0 行，
    比"不查时间"更糟。所以要按分句删掉整条条件。
    返回改过的 SQL；占位符出现在 WHERE 之外（SELECT/GROUP BY/LIMIT）或删不干净时返回 None，
    由调用方退回"缺参数"——宁可让用户补一句，也不拼出语义不明的 SQL。
    """
    text = str(sql or "")
    touched = False
    for name in names:
        for holder in ("'{" + name + "}'", "{" + name + "}"):
            if holder in text:
                text = text.replace(holder, "'" + _OPTIONAL_SENTINEL + "'")
                touched = True
    if not touched:
        return str(sql or "")
    try:
        tree = sqlglot.parse_one(text, read="mysql")
    except Exception as exc:  # noqa: BLE001
        log_error("templatize", "drop_conditions_parse_failed", exc)
        return None
    where = tree.find(exp.Where)
    if where is None:
        return None
    parts = _split_and(where.this)
    kept = [part for part in parts if _OPTIONAL_SENTINEL not in part.sql(dialect="mysql")]
    if len(kept) == len(parts):
        return None  # 占位符不在 WHERE 分句里（可能在 SELECT/GROUP BY/LIMIT）
    node = kept[0] if kept else exp.true()
    for extra in kept[1:]:
        node = exp.And(this=node, expression=extra)
    where.set("this", node)
    out = tree.sql(dialect="mysql")
    if _OPTIONAL_SENTINEL in out:
        return None
    return out


def _can_use_all(template: dict[str, Any], name: str) -> bool:
    """只有占位符确实位于可整段删除的 WHERE 条件里，才给这个实体开放“全部”。"""
    return drop_conditions(str(template.get("sql") or ""), [name]) is not None


def _time_end(name: str) -> bool:
    return str(name).lower().endswith(("_end", "_to"))


def _normalize_time_values(
    params: dict[str, Any], values: dict[str, str], granularity: str
) -> tuple[dict[str, str] | None, str]:
    """把自动模板的任意 *_start/*_end 时间槽归一成闭区间的日级端点。"""
    names = [
        name
        for name, spec in params.items()
        if str((spec or {}).get("type")) == "time" and str((values or {}).get(name) or "").strip()
    ]
    if not names:
        return {}, ""
    if granularity in {"quarter", "year"}:
        source_name = next((name for name in names if not _time_end(name)), names[0])
        if not str(values.get(source_name) or "").strip():
            return None, f"{params[source_name].get('label') or source_name}不能为空"
        for name, spec in params.items():
            if str((spec or {}).get("type")) == "time" and not values.get(name):
                values[name] = values[source_name]
        names = [
            name
            for name, spec in params.items()
            if str((spec or {}).get("type")) == "time" and str(values.get(name) or "").strip()
        ]
    normalized: dict[str, str] = {}
    for name in names:
        parsed = preset_lib._normalized_date(
            str(values.get(name) or ""), granularity, _time_end(name)
        )
        if not parsed:
            label = str((params.get(name) or {}).get("label") or name)
            return None, f"{label}与日期粒度不匹配，或选择了未来日期"
        normalized[name] = parsed
    return normalized, ""


def _replace_param(
    sql: str, name: str, spec: dict[str, Any], raw: str, cfg: dict[str, Any]
) -> tuple[str | None, str]:
    """替换一个参数（纯函数，从 bind 里抽出来：便于"先分类、再替换"的两趟处理）。"""
    kind = str(spec.get("type"))
    if kind == "op":
        if raw not in {str(item) for item in cfg.get("numeric_ops") or []}:
            return None, f"比较符不在白名单：{raw}"
        return sql.replace("{" + name + "}", raw), ""
    fragments = spec.get("fragments") or {}
    if fragments:
        if str(raw) not in fragments:
            return None, f"取值不在允许范围：{name}={raw}"
        return sql.replace("{" + name + "}", str(fragments[str(raw)])), ""
    if kind == "numeric" and not _NUMBER.match(str(raw)):
        return None, f"不是数值：{name}={raw}"
    if kind == "int":
        low, high = (spec.get("range") or [1, 50])[:2]
        if not str(raw).isdigit() or not (int(low) <= int(raw) <= int(high)):
            return None, f"取值不在允许范围：{name}={raw}"
    if kind == "time":
        text = _bind_time(name, spec, str(raw))
        if not text:
            return None, f"时间格式不对：{name}={raw}"
        return sql.replace("{" + name + "}", text.replace("'", "''")), ""
    # 多选用逗号拼接多个白名单值：**先按逗号拆开逐个校验**——以前拿整串去比白名单
    # 正则（里面没有逗号），多值一律被判"含非法字符"，多路线模板永远绑不上（P0-6）。
    parts = [part.strip() for part in str(raw).split(",") if part.strip()]
    if not parts or any(
        not re.match(r"^[0-9A-Za-z_\-:.\u4e00-\u9fff ]+$", part) for part in parts
    ):
        return None, "参数取值含非法字符"
    joined = "','".join(parts)
    # 占位符的形态要和挖掘时**完全一致**（_fill_fixed 同规则）：字符串类条件是
    # `= '{slot}'`，数值/名次类是裸的（`LIMIT {top_n}`、`area > {num_area}`）。
    # 以前这里无论类型都只替换带引号的形态，于是 TopN/阈值类模板回填必失败，
    # 回环验证直接把整条模板丢掉（docs/推荐模板链路Bug清单.md P1-5）。
    if str(kind) in {"entity", "enum", "time", "text_slot"}:
        return sql.replace("'" + "{" + name + "}" + "'", "'" + joined + "'"), ""
    return sql.replace("{" + name + "}", str(raw)), ""


def bind(template: dict[str, Any], values: dict[str, str]) -> tuple[str | None, str]:
    """把参数绑进 auto 模板：先按类型白名单校验，再替换占位符。

    本轮没提到的**可选条件**（时间/病害类型/路线/阈值…）不再让整条卡"缺参数"，
    而是把那一段 WHERE 条件整段去掉再查（方案 §4.2）——用户问"西乡段泛油有多少处"时，
    带 7 月过滤的模板会按"不限时间"查，而不是被丢掉或悄悄按 7 月查。
    """
    sql = str(template.get("sql") or "")
    cfg = _cfg()
    params = template.get("params") or {}
    values = dict(values or {})
    has_granularity = "date_granularity" in values
    granularity = str(values.pop("date_granularity", "") or "day").strip()
    if granularity not in {item["value"] for item in _DATE_GRANULARITY_OPTIONS}:
        return None, f"日期粒度不在允许范围：{granularity}"
    if has_granularity and any(
        str((spec or {}).get("type")) == "time" for spec in params.values()
    ):
        normalized, error = _normalize_time_values(params, values, granularity)
        if error:
            return None, error
        values.update(normalized or {})
    provided: dict[str, str] = {}
    dropped: list[str] = []
    for name, spec in params.items():
        text = str((values or {}).get(name) or "").strip()
        if text == _ALL_VALUE:
            if not _can_use_all(template, name):
                return None, f"参数无法安全设为全部：{spec.get('label') or name}"
            dropped.append(name)
            continue
        raw = coerce(spec, text)
        if not raw:
            if is_optional_param(name, spec):
                dropped.append(name)
                continue
            return None, f"缺少参数：{spec.get('label') or name}"
        provided[name] = raw
    for name, spec in params.items():
        if name not in provided:
            continue
        sql, error = _replace_param(sql, name, spec, provided[name], cfg)
        if error:
            return None, error
        sql = str(sql)
    if dropped:
        trimmed = drop_conditions(sql, dropped)
        if not trimmed:
            labels = "、".join(str((params.get(name) or {}).get("label") or name) for name in dropped)
            return None, f"缺参数，且这段条件没法安全去掉：{labels}"
        sql = trimmed
    if _PLACEHOLDER.search(sql):
        return None, "模板还有未绑定的参数"
    return sql, f"自动模板 {template.get('id')}"


def card_slots(
    template: dict[str, Any], values: dict[str, str], missing: list[str] | None = None
) -> list[dict[str, Any]]:
    """确认卡里的可改槽位（用户在卡片上改完再确认）。

    missing 里的槽是"问题里没提到或没抽到、但模板需要"的：不预填默认值，
    由用户在卡上选（选完才 bind 执行），避免系统替用户猜一个口径。
    """
    missing_names = set(missing or [])
    out: list[dict[str, Any]] = []
    for name, spec in (template.get("params") or {}).items():
        value = str((values or {}).get(name) or "")
        options = _options(spec)
        is_missing = name in missing_names
        if not value and options and not is_missing:
            value = str(options[0].get("value"))
        out.append(
            {
                "name": name,
                "label": _param_label(name, spec),
                "value": value,
                "options": options,
                "editable": True,
                "missing": is_missing,
            }
        )
    return out


def form_params(template: dict[str, Any]) -> list[dict[str, Any]]:
    """HTTP 表单/维护页用的参数描述：标签说人话 + 默认值来自这条模板的**已验证取值**。

    快捷查询与确认卡共用 `_options` 的取值逻辑（路线走"确实有数据的路线"、时间走月份、
    数值/比较符类没有枚举就返回空数组，前端据此渲染输入框/日期选择器）。
    """
    samples = template.get("samples") or []
    sample = dict((samples[0] or {}).get("values") or {}) if samples else {}
    out: list[dict[str, Any]] = []
    has_granularity = False
    for name, spec in (template.get("params") or {}).items():
        kind = str(spec.get("type") or "value")
        if kind == "time" and not has_granularity and not any(
            item.get("name") == "date_granularity" for item in out
        ):
            out.append(
                {
                    "name": "date_granularity",
                    "label": "日期粒度",
                    "type": "date_granularity",
                    "required": True,
                    "options": [dict(item) for item in _DATE_GRANULARITY_OPTIONS],
                    "value": "day",
                }
            )
            has_granularity = True
        options = _options(spec)
        if kind in {"entity", "enum"} and _can_use_all(template, name):
            if not any(str(item.get("value")) == _ALL_VALUE for item in options):
                options.append({"label": "全部", "value": _ALL_VALUE})
        item: dict[str, Any] = {
            "name": name,
            "label": _param_label(name, spec),
            "type": kind,
            "required": bool(spec.get("required", True)),
            "options": options,
        }
        default = _param_default(name, spec, sample.get(name, ""))
        if default:
            item["value"] = default
        hint = _param_hint(name, spec)
        if hint:
            item["hint"] = hint
        for key in ("column", "table", "form", "range", "op"):
            if spec.get(key) not in (None, "", [], {}):
                item[key] = spec[key]
        if spec.get("multi"):
            item["multi"] = True
        out.append(item)
    return out


def _param_label(name: str, spec: dict[str, Any]) -> str:
    """参数的**给人看**的名字：优先角色词，其次列的中文名，最后才退回列名/参数名。"""
    text = str(name or "")
    column = str(spec.get("column") or "")
    base = str(spec.get("label") or "").strip()
    stem = _COLUMN_LABELS.get(column) or base or column or text
    if text.endswith("_op"):
        head = _param_label(text[:-3], spec).removesuffix("比较符")
        return f"{head}·比较符"
    if str(spec.get("type")) == "time":
        # 起止两个槽同源（都叫"病害采集时间"），必须带上角色，否则用户不知道哪个填哪个
        return f"{stem}·{'从' if text.endswith('start') else '到'}"
    if text.endswith("_low"):
        return f"{stem}·下限"
    if text.endswith("_high"):
        return f"{stem}·上限"
    return stem


def _param_hint(name: str, spec: dict[str, Any]) -> str:
    """一句话说清这个槽填什么（快捷查询表单直接显示在输入框下面）。"""
    column = str(spec.get("column") or "")
    kind = str(spec.get("type") or "")
    if str(name or "").endswith("_op"):
        return "选比较符（大于/小于…）"
    if kind == "time":
        return "先选日期粒度；周按周一至周日、季度和年份自动覆盖完整区间"
    if kind == "int":
        return "取前几名（按这条模板的排序口径）"
    if kind == "numeric":
        if column in ("pile_distance", "pile_number"):
            return "桩号米数：K1+802 填 1802"
        if column == "confidence":
            return "AI 置信度，0~1 之间的小数（如 0.8）"
        if column == "area":
            return "面积，单位 ㎡"
        return "填数值"
    if kind in ("entity", "enum"):
        if column in ("routes_id", "route_id"):
            return "只列「确实有数据」的路线"
        if column in ("problem_type", "problem_code", "damage_type"):
            return "从病害类型字典里选"
        return "从库里的现有取值里选"
    return ""


def _param_default(name: str, spec: dict[str, Any], raw: str) -> str:
    """默认值 = 这条模板**验证过的那次取值**（用户不用猜要填什么）。

    时间槽返回**闭区间的日期**：`collection_time < '2026-07-31 00:00:00'` 在用户眼里是
    "到 7 月 30 日"，如果直接把 7/31 当默认值摆出来，卡片上写的就是"到 7/31"，
    跟这条模板回答的问题（7 月 30 日）对不上。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    kind = str(spec.get("type") or "")
    if kind == "time":
        day = text[:10]
        if _DATETIME.match(text) and not _MONTH_ONLY.match(text):
            if name.endswith("end") and str(spec.get("op") or "") == "<":
                day = _shift_day(day, -1)
            return day
        return text
    if kind in {"entity", "enum"}:
        return str(coerce(spec, text) or text)
    return text


def _shift_day(day: str, offset: int) -> str:
    try:
        base = datetime.strptime(str(day)[:10], "%Y-%m-%d")
    except ValueError:
        return str(day)[:10]
    return (base + timedelta(days=offset)).strftime("%Y-%m-%d")


def _bind_time(name: str, spec: dict[str, Any], raw: str) -> str | None:
    """时间槽 → SQL 字面量。

    · 精确到分钟的值原样用（老行为）；
    · 只给日期按**闭区间**补边界：起点 00:00:00、终点 `<` 补到次日 00:00:00、
      `<=` 补到当天 23:59:59 —— 否则用户在快捷查询里选"到 7 月 30 日"会整天查不到；
    · 只给月份仍退化成整月（和原来一致）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if _MONTH_ONLY.match(text):
        month = text[:7]
        year, mon = int(month[:4]), int(month[5:7])
        if name.endswith("end"):
            year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
        return f"{year:04d}-{mon:02d}-01 00:00:00"
    if not _DATETIME.match(text):
        return None
    if re.search(r"\d{2}:\d{2}", text):
        return text
    day = text[:10]
    if name.endswith("end"):
        if str(spec.get("op") or "") == "<":
            return f"{_shift_day(day, 1)} 00:00:00"
        return f"{day} 23:59:59"
    return f"{day} 00:00:00"
