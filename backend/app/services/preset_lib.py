"""预置查询模板库：唯一事实源是 data/preset_queries.json，这里提供加载/绑定/结构匹配。

见 docs/固定看板重构与收藏固化方案.md 第三章、第七章 §2、第八章 §1：

- 加载带 (mtime, size) 版本号：后端直接改 json 也会被下一次读取发现，不用重启；
- 绑定沿用原安全红线：参数值必须命中模板声明的 options 白名单（动态来源连库现查）；
- 结构匹配不走向量：按 shape / subject / 槽位覆盖去对子问题，模板表达不了的条件一律不匹配。
"""

from __future__ import annotations

import json
import re
import time
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import db_manager
from .logging_service import log_error, log_event


_PRESET_FILE = Path(__file__).resolve().parents[1] / "data" / "preset_queries.json"
# 绑定值只允许这些字符（中文给病害类型用）；白名单校验在它前面，这是第二道。
_SAFE_VALUE = re.compile(r"^[0-9A-Za-z_\-:\.\u4e00-\u9fff ]+$")
# 自由日期参数（起止日期）：没有枚举白名单，改按格式校验，注入风险由这个正则兜住。
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_VALUE = re.compile(r"^\d{4}-\d{2}$")
_QUARTER_VALUE = re.compile(r"^\d{4}-(?:0?[147]|10|Q[1-4])$", re.I)
_YEAR_VALUE = re.compile(r"^\d{4}$")
_VERSION: tuple[float, int] | None = None
_CACHE: dict[str, Any] = {}


def _file_version() -> tuple[float, int]:
    stat = _PRESET_FILE.stat()
    return (stat.st_mtime, stat.st_size)


def load(force: bool = False) -> dict[str, Any]:
    """读模板库；文件被改过（mtime/size 变了）就自动重载，不用重启。"""
    global _VERSION, _CACHE
    version = _file_version()
    if force or _VERSION != version or not _CACHE:
        data = json.loads(_PRESET_FILE.read_text(encoding="utf-8"))
        from .sql_engine import normalize_damage_code_joins

        if isinstance(data, dict):
            for item in data.get("queries") or []:
                if item.get("sql"):
                    item["sql"] = normalize_damage_code_joins(str(item["sql"]))
        _CACHE = data if isinstance(data, dict) else {}
        _VERSION = version
        log_event("preset", "INFO", "preset_reloaded", queries=len(_CACHE.get("queries") or []))
    return _CACHE


def query_by_id(query_id: str) -> dict[str, Any] | None:
    for item in load().get("queries") or []:
        if item.get("id") == query_id:
            return item
    return None


def month_label(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}年{int(parts[1])}月"
    return str(value)


def _dynamic_options(source: str) -> list[dict[str, str]]:
    """下拉框选项**每次连库现查**（去重），不写死在配置里。"""
    cached = _OPTIONS_CACHE.get(str(source))
    if cached and time.time() - cached[0] < _OPTIONS_TTL_SECONDS:
        return cached[1]
    options = clean_options(_query_dynamic_options(source))
    _OPTIONS_CACHE[str(source)] = (time.time(), options)
    return options


_CJK = re.compile(r"[\u4e00-\u9fff]")


def clean_options(options: list[dict[str, str]]) -> list[dict[str, str]]:
    """选项去重 + 去脏（docs/确认卡重复气泡耗时整改与污染模板库重置.md §三）。

    字典里同一个病害类型名对应多个 code（"一类多码"），还有测试留下的纯英文/数字脏值
    （实测 `adsad213223`/`eqweqw2313`/`848626` 会出现在病害类型下拉里，而且 97 条里有重名）。
    规则（结构性的，不是业务词表）：
    · 同名只留一条（先出现的优先）；
    · 只要还有其他选项带中文，就丢掉"纯 ASCII 名字"的那些（界面上的业务名都应该是中文）。
    """
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for item in options or []:
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not label and not value:
            continue
        key = label or value
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"label": label or value, "value": value or label})
    named = [item for item in deduped if _CJK.search(item["label"])]
    return named if named and len(named) >= max(2, len(deduped) // 4) else deduped


# 选项缓存：一次问答里同一份选项会被要好几次（每个候选 bind 一次、卡片再要一次），
# 以前每次都连库（库在 SSH 隧道后面，单次 ≈0.28 秒，问一次卡片能花掉 1.7 秒）。
# 60 秒 TTL：值仍然来自数据库（不是写死的），新增路线/月份最迟 1 分钟后出现在下拉框里。
_OPTIONS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_OPTIONS_TTL_SECONDS = 60.0


def _query_dynamic_options(source: str) -> list[dict[str, str]]:
    if source == "routes_with_data":
        rows = db_manager.query(
            "mysql",
            "SELECT r.id AS id, COALESCE(NULLIF(r.short_name, ''), r.full_name, r.code) AS name, "
            "COUNT(p.id) AS data_count "
            "FROM basic_facilities_routes r "
            "JOIN ai_problem p ON p.routes_id = r.id AND p.del_flag = 0 "
            "WHERE r.del_flag = 0 "
            "GROUP BY r.id, r.short_name, r.full_name, r.code "
            "ORDER BY data_count DESC, name",
        )["rows"]
        return [
            {
                "label": f"{row.get('name') or row.get('id')}（{row.get('data_count')} 处）",
                "value": str(row.get("id") or ""),
            }
            for row in rows
            if row.get("id")
        ]
    if source == "months":
        rows = db_manager.query(
            "mysql",
            "SELECT DISTINCT DATE_FORMAT(collection_time, '%Y-%m') AS month "
            "FROM ai_problem WHERE del_flag = 0 AND collection_time IS NOT NULL "
            "ORDER BY month DESC",
        )["rows"]
        return [
            {"label": month_label(str(row.get("month"))), "value": str(row.get("month"))}
            for row in rows
            if row.get("month")
        ]
    if source == "damage_types":
        rows = db_manager.query(
            "mysql",
            "SELECT DISTINCT problem_type AS type_name FROM ai_problem_base "
            "WHERE del_flag = 0 AND problem_type IS NOT NULL AND problem_type <> '' "
            "ORDER BY type_name",
        )["rows"]
        return [
            {"label": str(row.get("type_name")), "value": str(row.get("type_name"))}
            for row in rows
            if row.get("type_name")
        ]
    if source == "top_n":
        # 用户可能问"前 3 种"，所以 TopN 不能在配置里写死 5/10/20；
        # 这里给 1..50 的白名单（值仍然由后端提供，前端只能选不能填）。
        return [{"label": f"前 {i}", "value": str(i)} for i in range(1, 51)]
    if source == "asset_types":
        # 资产类型字典（冷门表模板用）：中文类型名来自库，不写死
        rows = db_manager.query(
            "mysql",
            "SELECT DISTINCT asset_type AS name FROM ai_asset_base "
            "WHERE del_flag = 0 AND asset_type IS NOT NULL AND asset_type <> '' "
            "ORDER BY asset_type",
        )["rows"]
        return [
            {"label": str(row.get("name")), "value": str(row.get("name"))}
            for row in rows
            if row.get("name")
        ]
    if source == "patrol_tasks":
        # 巡回任务编号（最新在前）：名称用"编号 · 巡查名称"
        rows = db_manager.query(
            "mysql",
            "SELECT t.task_number AS number, t.patrol_name AS name, t.create_time AS created "
            "FROM ai_maintain_patrol t WHERE t.del_flag = 0 AND t.task_number IS NOT NULL "
            "ORDER BY t.create_time DESC LIMIT 200",
        )["rows"]
        return [
            {
                "label": f"{row.get('number')}｜{str(row.get('name') or '')[:18]}",
                "value": str(row.get("number")),
            }
            for row in rows
            if row.get("number")
        ]
    if source == "sections_with_data":
        rows = db_manager.query(
            "mysql",
            "SELECT s.id AS id, "
            "COALESCE(NULLIF(s.full_name, ''), s.code, s.id) AS section_name, "
            "COALESCE(NULLIF(r.short_name, ''), r.full_name, r.code) AS route_name, "
            "COUNT(p.id) AS data_count "
            "FROM basic_facilities_sections s "
            "LEFT JOIN basic_facilities_routes r ON r.id = s.route_id AND r.del_flag = 0 "
            "JOIN ai_problem p ON p.sections_id = s.id AND p.del_flag = 0 "
            "WHERE s.del_flag = 0 "
            "GROUP BY s.id, s.full_name, s.code, r.short_name, r.full_name, r.code "
            "ORDER BY data_count DESC, section_name",
        )["rows"]
        return [
            {
                "label": (
                    f"{row.get('route_name') or '未关联路线'} · "
                    f"{row.get('section_name') or row.get('id')}（{row.get('data_count')} 处）"
                ),
                "value": str(row.get("id") or ""),
            }
            for row in rows
            if row.get("id")
        ]
    if source == "device_codes":
        # 设备号：只列真有数据的（按检出病害数倒序），避免摆一堆空设备
        rows = db_manager.query(
            "mysql",
            "SELECT p.device_code AS code, COUNT(*) AS data_count FROM ai_problem p "
            "WHERE p.del_flag = 0 AND p.device_code IS NOT NULL AND p.device_code <> '' "
            "GROUP BY p.device_code ORDER BY data_count DESC LIMIT 100",
        )["rows"]
        return [
            {
                "label": f"{row.get('code')}（{row.get('data_count')} 处）",
                "value": str(row.get("code")),
            }
            for row in rows
            if row.get("code")
        ]
    return []


def options_for(name: str, spec: dict[str, Any]) -> list[dict[str, str]]:
    source = str(spec.get("source") or "")
    if not source:
        options = list(spec.get("options") or [])
    else:
        try:
            options = list(_dynamic_options(source))
        except Exception as exc:  # noqa: BLE001
            log_error("preset", "dynamic_options_failed", exc, param=name, source=source)
            options = []
    if spec.get("allow_all") and not any(str(item.get("value")) == "__ALL__" for item in options):
        options.append({"label": "全部", "value": "__ALL__"})
    return options


def _strip_all_conditions(sql: str) -> str:
    """可选过滤参数选择“全部”后，移除对应过滤条件。"""
    text = str(sql or "")
    patterns = (
        r"\s+AND\s+[`\"\w.]+\s*=\s*'__ALL__'",
        r"\s+AND\s+[`\"\w.]+\s+IN\s*\(\s*'__ALL__'\s*\)",
        r"\s+AND\s+(?:DATE_FORMAT|YEAR|QUARTER|MONTH)\s*\([^)]*\)\s*=\s*'__ALL__'",
        r"\s+AND\s+[`\"\w.]+\s*>=\s*'__ALL__[^']*'",
        r"\s+AND\s+[`\"\w.]+\s*<\s*DATE_ADD\s*\(\s*'__ALL__'[^)]*\)",
    )
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.I)
    return text


def _normalized_date(value: str, granularity: str, end: bool) -> str | None:
    """把日/月/季度/年选择值归一成闭区间端点 YYYY-MM-DD。"""
    text = str(value or "").strip()
    today = date.today()
    if granularity == "day":
        if not _DATE_VALUE.fullmatch(text):
            return None
        selected = date.fromisoformat(text)
        return selected.isoformat() if selected <= today else None
    if granularity == "week":
        if not _DATE_VALUE.fullmatch(text):
            return None
        selected = date.fromisoformat(text)
        start = selected - timedelta(days=selected.isoweekday() - 1)
        if start > today:
            return None
        return (start + timedelta(days=6 if end else 0)).isoformat()
    if granularity == "month":
        if not _MONTH_VALUE.fullmatch(text):
            return None
        year, month = (int(item) for item in text.split("-"))
        try:
            start = date(year, month, 1)
            if start > today:
                return None
            day = monthrange(year, month)[1] if end else 1
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    if granularity == "quarter":
        if not _QUARTER_VALUE.fullmatch(text):
            return None
        year_text, quarter_text = text.split("-", 1)
        year = int(year_text)
        if quarter_text.upper().startswith("Q"):
            month = (int(quarter_text[1]) - 1) * 3 + 1
        else:
            month = int(quarter_text)
        if month not in {1, 4, 7, 10}:
            return None
        final_month = month + 2
        try:
            if date(year, month, 1) > today:
                return None
            day = monthrange(year, final_month)[1] if end else 1
            return date(year, final_month if end else month, day).isoformat()
        except ValueError:
            return None
    if granularity == "year":
        if not _YEAR_VALUE.fullmatch(text):
            return None
        year = int(text)
        try:
            if date(year, 1, 1) > today:
                return None
            return date(year, 12 if end else 1, 31 if end else 1).isoformat()
        except ValueError:
            return None
    return None


def bind(query: dict[str, Any], values: dict[str, str]) -> tuple[str | None, str]:
    """把参数绑进模板：只接受声明过的参数、只接受白名单里的值。失败返回 (None, 原因)。"""
    specs: dict[str, Any] = load().get("params") or {}
    sql = str(query.get("sql") or "")
    values = dict(values)
    granularity = str(values.get("date_granularity") or "").strip()
    if granularity in {"day", "week", "month", "quarter", "year"}:
        normalized: dict[str, str] = {}
        for name, end in (("date_from", False), ("date_to", True)):
            if not values.get(name):
                continue
            parsed = _normalized_date(str(values[name]), granularity, end)
            if not parsed:
                return None, f"{specs.get(name, {}).get('label') or name}与日期粒度不匹配"
            normalized[name] = parsed
        if normalized.get("date_from") and normalized.get("date_to"):
            if normalized["date_from"] > normalized["date_to"]:
                return None, "起始日期不能晚于结束日期"
        values.update(normalized)
    for name in query.get("params") or []:
        value = str(values.get(name) or "").strip()
        spec = specs.get(name) or {}
        if not value and spec.get("allow_all"):
            value = "__ALL__"
        if not value:
            return None, f"缺少参数：{spec.get('label') or name}"
        allowed = [str(item.get("value")) for item in options_for(name, spec)]
        if str(spec.get("type")) in {"time", "date"}:
            # 自由日期（起止日期）没有枚举白名单：按 YYYY-MM-DD 校验，别让任意串进 SQL
            if not _DATE_VALUE.match(value):
                return None, f"日期格式应为 YYYY-MM-DD：{name}={value}"
        elif value not in allowed:
            return None, f"参数取值不在允许范围：{name}={value}"
        if not _SAFE_VALUE.match(value):
            return None, "参数取值含非法字符"
        sql = sql.replace("{" + name + "}", value.replace("'", "''"))
    if "{" in sql and "}" in sql:
        return None, "模板还有未绑定的参数"
    return _strip_all_conditions(sql), ""


# 模板可以声明自己能回答什么（没有 match 段的模板不参与自动匹配，只供手动点）：
#   "match": {"shape": ["count"], "subject": ["disease"], "slots": ["route_id"]}
# slots 声明它能表达的过滤条件；子问题多出别的条件就一律不匹配。
def plan_slot_values(plan: dict[str, Any]) -> dict[str, str]:
    """把查询计划里的槽位翻成模板参数值（拿不到就缺省不填）。"""
    slots = plan.get("slots") or {}
    values: dict[str, str] = {}
    route = slots.get("route_match") or {}
    if route.get("id"):
        values["route_id"] = str(route["id"])
    # 多路线题（"A 和 B 分别是几车道"）：实体槽是**一个多值槽**，用逗号拼多个 id
    # （模板 SQL 里是 `r.id IN ('{route_id}')`，绑定按逗号展开）
    routes = slots.get("routes") or []
    ids = [str(item.get("id") or "") for item in routes if isinstance(item, dict)]
    ids = [item for item in ids if item]
    if len(ids) > 1:
        values["route_id"] = ",".join(ids)
    damage = slots.get("damage_matches") or []
    if damage:
        values["damage_type"] = str(damage[0].get("problem_type") or "")
    start = str(slots.get("time_start") or "")
    if len(start) >= 7:
        values["month"] = start[:7]
    if slots.get("up_or_down"):
        values["up_or_down"] = str(slots["up_or_down"])
    limit = (plan.get("semantic") or {}).get("limit")
    if limit:
        values["top_n"] = str(limit)
    # 冷门表模板用到的槽：设备号走语义层抽的 device_id（等价于 ai_problem.device_code），
    # 资产类型走语义条件里的 asset_type。取不到就不填 → 那条模板不参与自动匹配（只供手动点）。
    device = str(slots.get("device_id") or "").strip()
    if device:
        values["device_code"] = device
    for item in slots.get("semantic_conditions") or []:
        if str(item.get("field") or "") == "asset_type" and item.get("value"):
            values["asset_type"] = str(item["value"])
    return values


def match_templates(plan: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """第 1 道闸：按结构（shape + subject + 槽位覆盖）匹配模板，返回**有序候选**。

    宁可不匹配也不错匹配：模板声明的 slots 必须能覆盖子问题**全部**过滤条件，
    多出桩号区间/设备号/数值阈值/对比这类模板表达不了的条件就直接放弃。

    返回多条是为了"人工确认"（docs/确定性能力人工确认方案.md）：候选连同口径、
    参数当前值、可选项一起给用户看，由用户决定用哪条。
    """
    matches: list[dict[str, Any]] = []
    if str(plan.get("kind") or "mysql") != "mysql":
        return []
    if plan.get("comparison") or plan.get("multi_route"):
        return []
    required = set(plan.get("required_conditions") or [])
    group_by_hint = str((plan.get("slots") or {}).get("group_by") or "")
    unsupported = {
        "pile_range",
        "device_code",
        "has_image",
        "multi_route",
        "post_calc",
    }
    # "comparison" 只有在**没有分组维度**时才算"多对象对比"（模板表达不了）；
    # 有 group_by 说明它是"按某个维度分组"，模板的 GROUP BY 本来就满足。
    if "comparison" in required and not group_by_hint:
        return []
    if required & unsupported:
        return []
    # 语义层抽出的等值/数值条件：只有"路线名/病害类型/时间"这些模板能表达的才放过，
    # 出现收藏、工单状态、面积阈值这类模板没有的维度就一律不匹配（宁可不匹配不可错匹配）。
    expressible = {"route", "route_name", "damage_type", "problem_type_name", "collection_time"}
    semantic_fields = {
        str(item.get("field") or "")
        for item in (plan.get("slots") or {}).get("semantic_conditions") or []
    }
    if semantic_fields - expressible:
        return []
    shape = str(plan.get("shape") or "")
    subject = str(plan.get("subject") or "disease")
    group_by = group_by_hint
    semantic = plan.get("semantic") or {}
    plan_metrics = {str(item) for item in (semantic.get("metrics") or [])}
    values = plan_slot_values(plan)
    for query in load().get("queries") or []:
        match = query.get("match")
        if not isinstance(match, dict):
            continue
        if shape and match.get("shape") and shape not in list(match["shape"]):
            continue
        if match.get("subject") and subject not in list(match["subject"]):
            continue
        # 模板固定的分组维度：用户问的分组必须就是它（否则"按上下行分布"会被套成按类型分布）
        if match.get("group_by") and group_by not in list(match["group_by"]):
            continue
        # 反过来：用户要分组、模板却没声明分组维度 → 模板表达不了这个分组，不能命中
        # （否则"按工单状态分布"会被"图片覆盖情况"这种不分组模板接走）
        if group_by and not match.get("group_by"):
            continue
        if match.get("bucket_metric"):
            if str(semantic.get("bucket_metric") or "") not in list(match["bucket_metric"]):
                continue
        # 指标覆盖：用户要面积/平均，模板只出计数 → 不能匹配（否则答成"多少处"）
        if match.get("metrics") and not plan_metrics.issubset({str(i) for i in match["metrics"]}):
            continue
        if match.get("require_limit") and not values.get("top_n"):
            continue
        # 模板里写死的条件（如"泛油数量"这条模板固定只看泛油）：用户的条件必须与之一致
        fixed = {str(k): str(v) for k, v in (match.get("fixed") or {}).items()}
        conflict = False
        for field, value in fixed.items():
            if field == "damage_type":
                plan_types = [
                    str(item.get("problem_type"))
                    for item in (plan.get("slots") or {}).get("damage_matches") or []
                ]
                if not plan_types or value not in plan_types:
                    conflict = True
        if conflict:
            continue
        declared = [str(name) for name in (query.get("params") or [])]
        # 模板要用到的槽位必须都能从计划里拿到值
        if any(not values.get(name) for name in declared):
            continue
        covered = {str(item) for item in (match.get("slots") or [])} | set(fixed)
        # comparison 有 group_by 时由模板自己的 GROUP BY 满足；其余条件要么由槽位表达、
        # 要么模板里写死，多出来的（桩号区间/设备/数值阈值…）一律不匹配。
        structural = {"comparison"} if group_by_hint else set()
        if not required.issubset(covered | {"route", "damage_type", "time_range"} | structural):
            continue
        # 条件要么由模板参数表达（declared），要么模板里本来就写死了（fixed）
        if "route" in required and "route_id" not in declared and "route" not in fixed:
            continue
        if (
            "damage_type" in required
            and "damage_type" not in declared
            and "damage_type" not in fixed
        ):
            continue
        if "time_range" in required and "month" not in declared and "time_range" not in fixed:
            continue
        sql, reason = bind(query, values)
        if not sql:
            continue
        log_event(
            "preset",
            "INFO",
            "template_matched",
            template=str(query.get("id")),
            params="、".join(f"{k}={v}" for k, v in values.items() if k in declared),
        )
        score = 3.0
        reasons: list[str] = []
        if match.get("shape"):
            score += 2.0
            reasons.append(f"查询形态一致（{shape}）")
        if match.get("subject"):
            score += 2.0
            reasons.append(f"主体一致（{subject}）")
        if match.get("group_by"):
            score += 1.5
            reasons.append(f"分组维度一致（{group_by}）")
        # 轻量计划（探测用，不调语义模型）没有 metrics：这时别写"指标一致（）"，
        # 也别按"模板会多算"扣分——那是拿不到指标时的错觉。
        if match.get("metrics") and plan_metrics:
            score += 1.0
            reasons.append("指标一致（" + "、".join(sorted(plan_metrics)) + "）")
        if fixed:
            reasons.append("模板已写死：" + "、".join(f"{k}={v}" for k, v in fixed.items()))
        reasons.append("条件完整覆盖：" + ("、".join(sorted(required)) or "无条件"))
        score += min(len(declared), 3) * 0.5
        # 模板比用户要的指标多算一截（"有多少处"却把总面积/平均面积也算了）要扣分，
        # 让"刚好对上"的模板排在前面（否则 count 题会被面积统计模板压过去）。
        declared_metrics = {str(item) for item in (match.get("metrics") or [])}
        extra_metrics = (declared_metrics - plan_metrics) if plan_metrics else set()
        if extra_metrics:
            score -= 0.6 * len(extra_metrics)
            reasons.append("模板会多算：" + "、".join(sorted(extra_metrics)))
        matches.append(
            {
                "kind": "preset",
                "id": str(query.get("id")),
                "title": str(query.get("title") or ""),
                "question": str(query.get("question") or ""),
                "sql": sql,
                "scope": str(query.get("scope") or ""),
                "params": declared,
                "values": {name: values.get(name, "") for name in declared},
                "score": round(score, 2),
                "reasons": reasons,
            }
        )
    matches.sort(key=lambda item: -float(item.get("score") or 0))
    return matches[:limit]


def match_template(plan: dict[str, Any]) -> dict[str, Any] | None:
    """兼容旧调用：只取最匹配的一条。"""
    matches = match_templates(plan, limit=1)
    return matches[0] if matches else None


def _plan_flags(plan: dict[str, Any]) -> dict[str, bool]:
    """把计划压成几个布尔特征，供内置模板按语义声明匹配（不看用户原话）。"""
    slots = plan.get("slots") or {}
    semantic = plan.get("semantic") or {}
    metrics = {str(item) for item in (semantic.get("metrics") or [])}
    output_fields = {str(item) for item in (semantic.get("output_fields") or [])}
    required = set(plan.get("required_conditions") or [])
    # 数值/阈值条件要看**条件本身**是不是数值比较，不能"有语义条件"就算
    # （路线名/病害类型也是语义条件，会把阈值模板放进几乎所有问题里）
    numeric_ops = {">", ">=", "<", "<=", "between"}
    numeric_fields = {"area", "confidence", "disease_length", "area_width", "area_height", "pile_distance"}
    numeric_condition = any(
        str(item.get("op") or "") in numeric_ops
        or str(item.get("field") or "") in numeric_fields
        for item in slots.get("semantic_conditions") or []
    )
    # 用户明确给了数值阈值（"置信度 ≥0.7 / 低于 0.5"）→ 不能用"按中位数分档"那种模板
    from . import nl2sql  # 延迟导入：nl2sql 反过来 import 本模块

    explicit_threshold = bool(nl2sql.numeric_conditions(str(plan.get("message") or "")))
    return {
        "multi_route": bool(plan.get("multi_route")),
        "comparison": bool(plan.get("comparison")),
        "has_damage_filter": bool(slots.get("damage_matches")),
        "multi_metric": len(metrics) >= 2,
        "numeric_condition": numeric_condition,
        "explicit_threshold": explicit_threshold,
        # 「6月和7月各多少」这种多个并列时间段：按结构化槽位判断，不靠关键词选模板
        "multi_period": len(slots.get("months") or []) >= 2,
        "spatial_split": bool(slots.get("spatial_split")),
        "has_image": "has_image" in required,
        "pile_range": "pile_range" in required,
        "device_code": "device_code" in required,
        "route_length": bool(output_fields & {"route_length_km", "section_length_km"}),
        "patrol_route_overview": nl2sql.is_patrol_route_overview_plan(plan),
    }


def match_builtin_templates(plan: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """按**语义 plan** 匹配代码里的内置模板（resources/sql_templates/_index.yaml）。

    这是"快速通道"的选模板方式：只看 shape / 主体 / 分组维度 / 指标 / 派生指标与几个布尔特征，
    **不再用关键词去穷举触发**（docs/代码结构重构与模板外置方案.md §5.2 第 3 条）。
    声明里表达不了的（比如"尺寸"这种模糊指标）就不命中，交 Wren。
    """
    from .resource_loader import load_yaml

    kind = str(plan.get("kind") or "mysql")
    shape = str(plan.get("shape") or "")
    subject = str(plan.get("subject") or "disease")
    slots = plan.get("slots") or {}
    semantic = plan.get("semantic") or {}
    group_by = str(slots.get("group_by") or "")
    metrics = {str(item) for item in (semantic.get("metrics") or [])}
    derived = {str(item) for item in (semantic.get("derived") or [])}
    flags = _plan_flags(plan)
    route = slots.get("route_match") or {}

    matches: list[dict[str, Any]] = []
    registry = load_yaml("sql_templates/_index.yaml", {}) or {}
    for item in registry.get("templates") or []:
        if str((item or {}).get("source")) != "builtin":
            continue
        spec = item.get("match") or {}
        builder = str(item.get("builder") or "")
        if not builder:
            continue

        def _satisfied(rule: dict[str, Any]) -> bool:
            if rule.get("kinds") and kind not in list(rule["kinds"]):
                return False
            if rule.get("shapes") and shape not in list(rule["shapes"]):
                return False
            if rule.get("subjects") and subject not in list(rule["subjects"]):
                return False
            if rule.get("group_by") and group_by not in list(rule["group_by"]):
                return False
            if rule.get("metrics") and not (metrics & set(rule["metrics"])):
                return False
            if rule.get("derived") and not (derived & set(rule["derived"])):
                return False
            required = {str(f) for f in (rule.get("flags") or {}).get("require") or []}
            if not all(flags.get(name) for name in required):
                return False
            require_any = [str(f) for f in (rule.get("flags") or {}).get("require_any") or []]
            if require_any and not any(flags.get(name) for name in require_any):
                return False
            forbidden = {str(f) for f in (rule.get("flags") or {}).get("forbid") or []}
            if any(flags.get(name) for name in forbidden):
                return False
            return True

        # match_alt：同一模板的第二套语义条件（例如"按路线比平均面积"没有 multi_route 标记，
        # 但同样是"多对象比指标"）
        alt = item.get("match_alt") or {}
        if not _satisfied(spec) and not (alt and _satisfied(alt)):
            continue
        required_flags = {str(f) for f in (spec.get("flags") or {}).get("require") or []}
        score = 3.0 + 1.0 * bool(spec.get("shapes")) + 1.0 * bool(spec.get("subjects"))
        score += 0.5 * bool(spec.get("group_by")) + 0.5 * bool(required_flags)
        reasons = [f"plan 语义匹配：shape={shape or '-'}，主体={subject}"]
        if group_by:
            reasons.append(f"分组={group_by}")
        if metrics:
            reasons.append("指标=" + "、".join(sorted(metrics)))
        matches.append(
            {
                "id": str(item.get("id")),
                "title": str(item.get("title") or item.get("id")),
                "builder": builder,
                "score": round(score, 2),
                "reasons": reasons,
            }
        )
    matches.sort(key=lambda entry: -float(entry.get("score") or 0))
    return matches[:limit]
