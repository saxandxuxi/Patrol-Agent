"""条件存在性校验：范围校准（L2）+ 空结果诊断/条件消融（L3）。

设计依据 docs/评测方案V5-桩号范围校准与抽象场景篇.md：
用户给的条件本身可能超出实际数据范围（台账说 5.6km，巡检只采了 0.9km），
这时既不能硬查、也不能硬答 0，而要：提醒 + 给出实际范围 + 给出范围内统计。
"""

from __future__ import annotations

import time
import json
import re
from typing import Any

from . import parsers
from .db import db_manager

_FACTS_TTL_SECONDS = 300
_facts_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# 「6月」「7月」「12 月」这类月份记号，用来判断一句话里问了几段时间
_MONTH_TOKENS = re.compile(r"(\d{1,2})\s*月")


def fmt_pile(meter: Any) -> str:
    try:
        value = int(float(meter))
    except (TypeError, ValueError):
        return str(meter)
    return f"K{value // 1000}+{value % 1000:03d}"


def _query(sql: str) -> list[dict[str, Any]]:
    """校准是"锦上添花"的层：查不通就安静降级，不能让整次回答失败。"""
    try:
        return db_manager.query("mysql", sql)["rows"]
    except Exception:
        return []


def route_facts(route_id: str) -> dict[str, Any]:
    """一条路线的台账信息 + 实际病害数据范围（5 分钟缓存）。"""
    key = f"route:{route_id}"
    cached = _facts_cache.get(key)
    if cached and time.time() - cached[0] < _FACTS_TTL_SECONDS:
        return cached[1]

    # 台账 + 路段合成**一条** JOIN（库在 SSH 隧道后面，每多一次往返 ≈0.35s）：
    # 原来要 3 次往返，现在 2 次；剩下的冷启动成本由启动预热 `warm_cache()` 兜掉。
    # 注意 db_manager 的所有查询共用一把锁（paramiko 通道复用），并行查询没有收益。
    ledger = (_query(
        "SELECT r.short_name, r.start_num, r.end_num, r.distance, r.lane_count, "
        "s.start_meter_total, s.end_meter_total, s.tech_grade, s.surface_type, s.surface_width "
        "FROM basic_facilities_routes r "
        "LEFT JOIN basic_facilities_sections s ON s.route_id = r.id AND s.del_flag = 0 "
        f"WHERE r.id = '{route_id}' LIMIT 1"
    ) or [{}])[0]
    data = (_query(
        "SELECT MIN(p.pile_distance) AS min_pile, MAX(p.pile_distance) AS max_pile, "
        "MIN(p.collection_time) AS min_time, MAX(p.collection_time) AS max_time, COUNT(*) AS cnt "
        f"FROM ai_problem p WHERE p.del_flag = 0 AND p.routes_id = '{route_id}'"
    ) or [{}])[0]
    route, section = ledger, ledger

    facts = {
        "route": route.get("short_name"),
        "ledger_start": route.get("start_num"),
        "ledger_end": route.get("end_num"),
        "distance": route.get("distance"),
        "lane_count": route.get("lane_count"),
        "section_start": section.get("start_meter_total"),
        "section_end": section.get("end_meter_total"),
        "tech_grade": section.get("tech_grade"),
        "surface_type": section.get("surface_type"),
        "surface_width": section.get("surface_width"),
        "data_min_pile": data.get("min_pile"),
        "data_max_pile": data.get("max_pile"),
        "data_min_time": data.get("min_time"),
        "data_max_time": data.get("max_time"),
        "data_count": data.get("cnt"),
    }
    _facts_cache[key] = (time.time(), facts)
    return facts


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def warm_cache(limit: int = 8) -> int:
    """预热最常用路线的校准事实（后台跑，不阻塞任何请求）。

    为什么要预热：`route_facts` 是 5 分钟缓存，但**第一条**问某条路的问题要现场查
    3 张表（库在 SSH 隧道后面，≈1 秒）。启动后把这几个"有病害数据的路线"先查好，
    用户问到时就是缓存命中、不额外花时间。失败静默（校准本来就是锦上添花）。
    """
    warmed = 0
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT r.id AS id, COUNT(p.id) AS cnt FROM basic_facilities_routes r "
            "JOIN ai_problem p ON p.routes_id = r.id AND p.del_flag = 0 "
            "WHERE r.del_flag = 0 GROUP BY r.id ORDER BY cnt DESC LIMIT %d" % int(limit),
        )["rows"]
    except Exception:  # noqa: BLE001
        return 0
    for row in rows:
        route_id = str(row.get("id") or "")
        if not route_id:
            continue
        try:
            route_facts(route_id)
            warmed += 1
        except Exception:  # noqa: BLE001
            continue
    try:
        global_data_range()
    except Exception:  # noqa: BLE001
        pass
    return warmed


def global_data_range() -> dict[str, Any]:
    """全库病害的时间/桩号实际范围（无路线条件时用）。"""
    cached = _facts_cache.get("global")
    if cached and time.time() - cached[0] < _FACTS_TTL_SECONDS:
        return cached[1]
    row = (_query(
        "SELECT MIN(p.collection_time) AS min_time, MAX(p.collection_time) AS max_time, "
        "MIN(p.pile_distance) AS min_pile, MAX(p.pile_distance) AS max_pile, COUNT(*) AS cnt "
        "FROM ai_problem p WHERE p.del_flag = 0"
    ) or [{}])[0]
    _facts_cache["global"] = (time.time(), row)
    return row


def route_names() -> set[str]:
    """库里所有路线名（用于校验 SQL 里出现的路线名是不是用户要的）。"""
    cached = _facts_cache.get("route_names")
    if cached and time.time() - cached[0] < _FACTS_TTL_SECONDS:
        return cached[1]
    rows = _query(
        "SELECT DISTINCT short_name, full_name FROM basic_facilities_routes "
        "WHERE del_flag = 0 AND short_name <> '' LIMIT 5000"
    )
    names = {
        str(row.get(key))
        for row in rows
        for key in ("short_name", "full_name")
        if row.get(key)
    }
    _facts_cache["route_names"] = (time.time(), names)
    return names


def check(plan: dict[str, Any]) -> dict[str, Any] | None:
    """执行前检查：用户给的范围是否落在实际数据范围内。

    返回 {"coverage": "none"|"partial", "kind": ..., "message": ..., "facts": ...}
    或 None（条件都在范围内）。
    """
    slots = plan.get("slots", {})
    if plan.get("kind") != "mysql" or plan.get("subject", "disease") != "disease":
        return None
    route = slots.get("route_match") or {}
    route_id = route.get("id")
    if not route_id:
        # 没有路线时，用全库范围判断时间条件是否越界（例如"2024年的病害"）
        if slots.get("time_start"):
            overall = global_data_range()
            start = str(slots["time_start"])[:19]
            end = str(slots.get("time_end") or "")[:19]
            lo = str(overall.get("min_time") or "")[:19]
            hi = str(overall.get("max_time") or "")[:19]
            if lo and (start > hi or (end and end <= lo)):
                return {
                    "coverage": "none",
                    "kind": "time",
                    "message": (
                        f"全库病害数据的实际时间范围是 {lo} ~ {hi}，您问的时间段没有数据"
                    ),
                    "facts": {
                        "ledger_start": "—",
                        "ledger_end": "—",
                        "data_min_pile": overall.get("min_pile"),
                        "data_max_pile": overall.get("max_pile"),
                        "data_count": overall.get("cnt"),
                    },
                }
        return None
    facts = route_facts(str(route_id))
    data_min = _num(facts.get("data_min_pile"))
    data_max = _num(facts.get("data_max_pile"))

    pile_start, pile_end = slots.get("pile_start"), slots.get("pile_end")
    if data_min is not None and pile_start is not None and pile_end is not None:
        coverage = "full"
        if pile_end < data_min or pile_start > data_max:
            coverage = "none"
        elif pile_start < data_min or pile_end > data_max:
            coverage = "partial"
        if coverage != "full":
            span = f"{fmt_pile(data_min)}~{fmt_pile(data_max)}"
            ask = f"{fmt_pile(pile_start)}~{fmt_pile(pile_end)}"
            message = (
                f"该路段台账标的是 {facts.get('ledger_start')}~{facts.get('ledger_end')}，"
                f"但当前巡检病害数据实际只覆盖 {span}，您问的 {ask} "
                + ("没有病害记录" if coverage == "none" else "只有一部分落在实际范围内")
            )
            return {"coverage": coverage, "kind": "pile", "message": message, "facts": facts}

    time_start = slots.get("time_start")
    if time_start:
        # 一问多个时间段（"6月和7月分别有多少"）时，只有一段没数据不代表整题无解：
        # 那段本来就该如实答 0，别拿"您问的时间段没有数据"把整题顶掉。
        if len(set(_MONTH_TOKENS.findall(plan.get("message", "")))) >= 2:
            return None
        data_time_min = str(facts.get("data_min_time") or "")
        data_time_max = str(facts.get("data_max_time") or "")
        if data_time_min and data_time_max:
            start = str(time_start)[:19]
            end = str(slots.get("time_end") or "")[:19]
            if start > data_time_max or (end and end <= data_time_min):
                return {
                    "coverage": "none",
                    "kind": "time",
                    "message": (
                        f"{facts.get('route')} 的病害数据实际覆盖 "
                        f"{data_time_min[:19]} ~ {data_time_max[:19]}，您问的时间段没有数据"
                    ),
                    "facts": facts,
                }
    return None


def _range_stats(plan: dict[str, Any], facts: dict[str, Any]) -> str:
    """实际范围内的统计（用来替代"硬答 0"）。"""
    route = (plan["slots"].get("route_match") or {}).get("id")
    if not route:
        return ""
    rows = _query(
        "SELECT CASE WHEN b.problem_type IS NOT NULL AND b.problem_type <> 'NULL' "
        "THEN b.problem_type ELSE '未识别类型' END AS type, COUNT(*) AS count "
        "FROM ai_problem p JOIN basic_facilities_routes r ON p.routes_id = r.id "
        "LEFT JOIN (SELECT DISTINCT id, problem_type FROM ai_problem_base "
        "WHERE problem_type IS NOT NULL "
        "AND (del_flag = 0 OR del_flag IS NULL)) b ON p.problem_base_id = b.id "
        f"WHERE p.del_flag = 0 AND r.del_flag = 0 AND r.id = '{route}' "
        "GROUP BY type ORDER BY count DESC LIMIT 4"
    )
    if not rows:
        return ""
    parts = [f"{row['type']} {row['count']} 处" for row in rows]
    total = sum(int(row["count"]) for row in rows)
    return "；".join(parts) + f"，合计 {total} 处"


def calibrated_reply(plan: dict[str, Any], info: dict[str, Any]) -> str:
    facts = info["facts"]
    lines = [info["message"] + "。"]
    if info["kind"] == "time":
        # 时间越界：只说明**数据实际覆盖到哪**，并给一句下一步建议。
        # ⚠️ 这里**不能**再拼"实际范围内的统计"——一边说"您问的时间段没有数据"、一边又给
        # 一坨全量统计，用户只会更糊（方案 §12 说的"回复矛盾"，实测原文就是这个）。
        lo = str(facts.get("data_min_time") or "")[:10]
        hi = str(facts.get("data_max_time") or "")[:10]
        if lo and hi:
            lines.append(f"（库里的病害数据实际覆盖 {lo} ~ {hi}，共 {facts.get('data_count')} 条）")
        else:
            lines.append(f"（库里病害 {facts.get('data_count')} 条）")
        lines.append("建议换一个落在数据覆盖范围内的时间段；想看全部时间的数据，直接去掉时间条件再问一次。")
        return "\n".join(lines)
    scope = f"{fmt_pile(facts.get('data_min_pile'))}~{fmt_pile(facts.get('data_max_pile'))}"
    lines.append(f"（台账：{facts.get('ledger_start')}~{facts.get('ledger_end')}，"
                 f"病害数据实际范围：{scope}，共 {facts.get('data_count')} 处）")
    # 桩号越界（用户问的桩号段超出实际数据）：这里**可以**给实际范围内的统计——
    # 同一路段、只是区间不同，用户看实际分布是有用的。
    stats = _range_stats(plan, facts)
    if stats:
        lines.append(f"实际范围内的统计：{stats}。")
    return "\n".join(lines)


def explain_zero(plan: dict[str, Any]) -> str | None:
    """执行后结果为 0 时给出原因，而不是只回"共 0 处"。"""
    # 这个解释只取决于计划本身（路线/类型/时间等条件），同样的计划结果也一样：
    # 缓存 5 分钟，避免"同一个问题问两遍"时又连库探测三四次。
    key = "explain_zero:" + json.dumps(_plan_signature(plan), ensure_ascii=False, sort_keys=True)
    cached = _facts_cache.get(key)
    if cached and time.time() - cached[0] < _FACTS_TTL_SECONDS:
        return cached[1]
    text = _explain_zero_uncached(plan)
    _facts_cache[key] = (time.time(), text)
    return text


def _plan_signature(plan: dict[str, Any]) -> dict[str, Any]:
    """计划的"影响结论的那几个条件"，用来当缓存键（不带整份 plan，避免噪声字段）。"""
    slots = plan.get("slots") or {}
    return {
        "subject": plan.get("subject"),
        "route": (slots.get("route_match") or {}).get("id"),
        "damage": [str(item.get("problem_type")) for item in slots.get("damage_matches") or []],
        "time_start": slots.get("time_start"),
        "time_end": slots.get("time_end"),
        "pile_start": slots.get("pile_start"),
        "pile_end": slots.get("pile_end"),
        "conditions": [
            [str(item.get("field")), str(item.get("op")), str(item.get("value"))]
            for item in slots.get("semantic_conditions") or []
        ],
    }


def _explain_zero_uncached(plan: dict[str, Any]) -> str | None:
    slots = plan.get("slots", {})
    subject = plan.get("subject", "disease")
    if subject != "disease":
        return None

    # ① 病害类型：字典里有、数据里没有
    damage = slots.get("damage_matches") or []
    if damage and not slots.get("pile_start") and not slots.get("time_start"):
        types = "、".join(item["problem_type"] for item in damage)
        all_types = _query(
            "SELECT COUNT(DISTINCT problem_type) AS cnt FROM ai_problem p "
            "JOIN ai_problem_base b ON p.problem_base_id = b.id WHERE p.del_flag = 0"
        )
        present = _query(
            "SELECT COUNT(*) AS cnt FROM ai_problem p JOIN ai_problem_base b "
            "ON p.problem_base_id = b.id WHERE p.del_flag = 0 "
            f"AND b.problem_type IN ({', '.join(chr(39) + t + chr(39) for t in types.split('、'))})"
        )
        if present and int(present[0]["cnt"]) == 0:
            kinds = all_types[0]["cnt"] if all_types else "?"
            overall = _query(
                "SELECT COUNT(*) AS cnt FROM ai_problem WHERE del_flag = 0"
            )
            total = overall[0]["cnt"] if overall else "?"
            return (
                f"0 处。「{types}」在病害字典里是存在的（当前数据里出现过的类型有 {kinds} 种），"
                f"但巡检数据里一条都没有采到；全库病害共 {total} 条。"
            )

    # ② 路线 + 时间：该路线数据从什么时候开始
    route = slots.get("route_match") or {}
    if slots.get("time_start") and route.get("id"):
        facts = route_facts(str(route["id"]))
        data_min = str(facts.get("data_min_time") or "")[:19]
        data_max = str(facts.get("data_max_time") or "")[:19]
        if data_min:
            return (
                f"0 处。{facts.get('route')} 的病害数据实际覆盖 {data_min} ~ {data_max}，"
                f"您问的时间段没有记录（该路线数据从 {data_min[:10]} 才开始）。"
            )

    # ③ 桩号区间越界
    if slots.get("pile_start") is not None and route.get("id"):
        facts = route_facts(str(route["id"]))
        span = f"{fmt_pile(facts.get('data_min_pile'))}~{fmt_pile(facts.get('data_max_pile'))}"
        return (
            f"0 处。{facts.get('route')} 的病害数据实际只覆盖 {span}（台账标注 "
            f"{facts.get('ledger_start')}~{facts.get('ledger_end')}），您问的桩号区间没有记录。"
        )
    return None


_NUMERIC_LABELS = (
    ("JSON_EXTRACT", "置信度"),
    ("disease_length", "病害长度"),
    ("p.area", "面积"),
    ("p.disease_length", "病害长度"),
)


def explain_numeric_zero(plan: dict[str, Any], condition: str) -> str | None:
    """数值条件答出 0 时，报出该字段的实际取值范围（V5-14/15 要求）。"""
    match = re.match(r"^(.+?)\s*(>=|<=|>|<|=)\s*(.+)$", condition)
    if not match:
        return None
    expression, _, asked = match.group(1).strip(), match.group(2), match.group(3).strip()
    name = next((label for key, label in _NUMERIC_LABELS if key in expression), None)
    if not name:
        return None
    where = ["p.del_flag = 0"]
    route = (plan["slots"].get("route_match") or {}).get("id")
    joins = ""
    if route:
        joins = "JOIN basic_facilities_routes r ON p.routes_id = r.id"
        where.append("r.del_flag = 0")
        where.append(f"r.id = '{route}'")
    damage = plan["slots"].get("damage_matches") or []
    if damage:
        joins += (
            " JOIN (SELECT DISTINCT id, problem_type FROM ai_problem_base "
            "WHERE problem_type IS NOT NULL "
            "AND (del_flag = 0 OR del_flag IS NULL)) b ON p.problem_base_id = b.id"
        )
        values = ", ".join("'" + item["problem_type"].replace("'", "''") + "'" for item in damage)
        where.append(f"b.problem_type IN ({values})")
    if "JSON" in expression:
        where.append("p.pic_position IS NOT NULL")
    # 取值范围是按"字段 + 路线 + 病害类型"稳定的事实，5 分钟内不变：
    # 以前每次零结果都重查一遍（SSH 隧道下 0.3 秒/次，实测一次确认要 4 次），
    # 给出"实际范围"这段说明是用户要的，但没必要每次都连库。
    cache_key = f"range:{expression}|{' AND '.join(where)}"
    cached = _facts_cache.get(cache_key)
    if cached and time.time() - cached[0] < _FACTS_TTL_SECONDS:
        rows = cached[1]
    else:
        rows = _query(
            f"SELECT MIN({expression}) AS lo, MAX({expression}) AS hi FROM ai_problem p {joins} "
            f"WHERE {' AND '.join(where)}"
        )
        _facts_cache[cache_key] = (time.time(), rows)
    if not rows or rows[0].get("lo") is None:
        return None
    lo, hi = rows[0]["lo"], rows[0]["hi"]
    return (
        f"0 处。{name}的实际取值范围是 {lo} ~ {hi}，您问的「{name} {match.group(2)} {asked}」超出了这个范围"
        f"（最大就是 {hi}）。"
    )
