from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from ..config import settings
from ..store import delete_memory, get_memory, set_memory
def _session_key(session_id: str) -> str:
    return f"session:{session_id}"


def _pick_text(value: Any, limit: int = 40) -> str:
    """取一个干净的短文本（模型返回的字段可能是 None/数字/列表）。"""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()[:limit]


def _llm_complete_followup(message: str, session_id: str | None) -> dict[str, Any] | None:
    """对话理解 + 补全：返回 {"question", "relation", "multi_object", "objects"}。

    docs/项目智能化升级整体方案.md 升级2：从"条件补全"升级到"对话理解"——
    不光继承条件，还要判断这句话和前面是什么关系（继承/追问/纠正/对比/换话题），
    以及"这轮是不是在问多个对象之间的事"（对比、优先级），后者的对象必须从历史里带全
    （实测"下半年经费该优先投哪条路"丢了另一条路，答出"其他路线没有病害记录"）。
    返回 None 表示理解不出来，调用方回退到规则补全。
    """
    if not session_id:
        return None
    try:
        from ..store import get_conversation_messages
        from . import prompts
        from .llm import chat_json, role_model

        history: list[str] = []
        for item in (get_conversation_messages(session_id) or [])[-6:]:
            text = str(item.get("content") or "").strip().replace("\n", " ")
            if not text:
                continue
            role = "用户" if item.get("role") == "user" else "助手"
            history.append(f"{role}：{text[:120]}")
        if not history:
            return None
        context = load_session(session_id) or {}
        # 上一轮比较过的对象（路线）要带进提示词，多对象问题才不会漏掉一方
        recent = [str(item.get("name")) for item in (context.get("recent_routes") or []) if item.get("name")]
        if recent:
            history.append(f"（前几轮出现过的对象：{'、'.join(recent[:3])}）")
        # 上一轮链式推理算出来的关键值（日期/编号/路线/类型）：用户说"那天/那次/它"时直接用
        chain_values = context.get("chain_values") or {}
        if isinstance(chain_values, dict) and chain_values:
            pairs = "；".join(f"{key}={value}" for key, value in list(chain_values.items())[:6])
            history.append(f"（上一轮算出的关键值：{pairs}）")
        # 上一轮如果是"唯一一条"结果（最多的那次巡检/最高的那条病害），把它记下来，
        # 用户说"那次/那个"时要把这个 id 作为过滤条件写进补全后的问题（Bug 6）。
        entity = context.get("last_entity") or {}
        entity_hint = ""
        if isinstance(entity, dict) and entity.get("id"):
            entity_hint = (
                f"（上一轮找到的具体对象：{entity.get('field')} = {entity.get('id')}；"
                "用户说「那次/那个/它」「密度最高的那条路」「最多的那次巡检」时，"
                "**直接把它当成已知条件写进问题，不要重新算一遍**："
                "如果它是路线名，就补成「宝安大道西乡段…」；如果是巡检编号/ID，"
                "就补成「巡查编号 xxx 那次巡检…」。"
                "**同时要保留前面几轮已经确定的路线/时间等条件**——"
                "这个对象是**追加**的过滤条件，不是用来替换路线和时间的"
                "（例如「宝安大道西乡段7月，巡查编号 xxx 那次巡检发现的病害明细」））"
            )
        data = chat_json(
            prompts.messages(
                "dialogue_understanding",
                history="\n".join(history) + entity_hint,
                message=message,
            ),
            # 长对话理解是短板环节（丢条件/理解反都在这里），用语义理解模型
            model=role_model("semantic"),
        )
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rewritten = str(data.get("full_question") or "").strip()
    # 只接受"看起来像一句话问题"的结果，避免把说明性文字当查询
    rewritten = rewritten.splitlines()[0].strip() if rewritten else ""
    if not (2 <= len(rewritten) <= 120) or "根据上一轮" in rewritten:
        return None
    from .logging_service import log_event

    relation = _pick_text(data.get("relation_type"), 20).lower()
    if relation not in {"inherit", "followup", "correction", "compare", "new_topic"}:
        relation = ""
    objects = [
        _pick_text(item, 40)
        for item in (data.get("objects") or [])
        if _pick_text(item, 40)
    ][:4]
    dropped = [
        _pick_text(item, 40)
        for item in (data.get("dropped_conditions") or [])
        if _pick_text(item, 40)
    ][:4]
    return {
        "question": rewritten,
        "relation": relation,
        "multi_object": bool(data.get("multi_object")),
        "objects": objects,
        "dropped": dropped,
    }


def pool_entries_from_rows(
    question: str, rows: list[dict[str, Any]], damage: str = ""
) -> list[dict[str, Any]]:
    """复合问题的子结果也进对话池（主体→数值）。

    长对话报告场景8：第一轮"西乡段和恒明珠的泛油各有多少处？"走的是复合分支，
    结果没进池子，于是下一句"哪条路更多？多多少？"找不到可比较的对象。
    """
    if not rows or len(rows) > 20:
        return []
    label_key = next(
        (key for key in ("type", "route_name", "short_name", "name") if key in rows[0]), ""
    )
    value_key = next(
        (key for key in ("count", "cnt", "total_count", "disease_count") if key in rows[0]), ""
    )
    if not label_key or not value_key:
        return []
    entries: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get(label_key) or "").strip()
        value = row.get(value_key)
        if not name or value in (None, "", "NULL"):
            continue
        if not re.match(r"^-?\d+(?:\.\d+)?$", str(value)):
            continue
        entries.append({"name": name, "value": str(value), "damage": damage})
    return entries


def pool_entries(plan: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """本轮结果进"对话池"：主体（路线/路段/类型…）+ 数值，供下一句"哪边多/加起来"用。

    池子按**口径**去重（见 save_session 里的 pool_key）：同一个主体换个时间/病害类型
    是两条不同的口径，不能互相顶掉。
    """
    rows = list(result.get("rows") or [])
    if not rows:
        return []
    slots = plan.get("slots") or {}
    damage = ""
    matches = slots.get("damage_matches") or []
    if matches:
        damage = "、".join(str(item.get("problem_type") or "") for item in matches if item.get("problem_type"))
    elif slots.get("damage_type"):
        damage = str(slots["damage_type"])
    entries = pool_entries_from_rows(str(plan.get("message") or ""), rows, damage)
    # 单行结果（"西乡段泛油有多少处"）也要进池子：主体取路线名，数值取那一个指标。
    if not entries and len(rows) == 1:
        row = rows[0]
        value = next(
            (
                str(row.get(key))
                for key in ("count", "cnt", "total_count", "disease_count", "distinct_pile_count")
                if row.get(key) not in (None, "", "NULL")
            ),
            "",
        )
        name = str(
            (slots.get("route_match") or {}).get("short_name")
            or slots.get("route")
            or ""
        ).strip()
        if name and value and re.match(r"^-?\d+(?:\.\d+)?$", value):
            entries.append({"name": name, "value": value, "damage": damage})
    # 口径用现成的 pool_scope（路线/时间/病害/桩号/设备 的 dict）——池子按它去重
    scope = pool_scope(plan)
    if damage:
        scope.setdefault("damage", damage)
    for entry in entries:
        entry["scope"] = dict(scope)
    return entries


def load_session(session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {}
    value = get_memory(_session_key(session_id))
    return value if isinstance(value, dict) else {}


def save_session(session_id: str | None, context: dict[str, Any] | None) -> None:
    if not session_id or not context:
        return
    previous = load_session(session_id)
    # 口径按「最近一轮」算：这一轮没提到的条件要清掉。
    # 以前是只加不减，问过"西乡段泛油"之后，十几轮后随便追问一句都会
    # 被塞进"宝安大道西乡段 + 泛油"，然后答出一个跟问题无关的数。
    merged = dict(previous)
    for key, value in context.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    # 记住最近提到的路线（不只最后一条），"两边加起来呢"这类问题要靠它。
    merged["updated_at"] = datetime.now().isoformat(timespec="seconds")
    route_id = context.get("route_id")
    route_name = context.get("route")
    if route_id and route_name:
        history = [
            item
            for item in (previous.get("recent_routes") or [])
            if str(item.get("id")) != str(route_id)
        ]
        history.insert(0, {"id": str(route_id), "name": str(route_name)})
        merged["recent_routes"] = history[:3]
    # 对话状态池：本轮各主体的数值（路线→条数），"两边加起来/哪边多/多多少"靠它算。
    entries = context.get("pool_entries") or []
    if entries:
        pool = [item for item in (previous.get("subject_pool") or [])]
        for entry in entries:
            if not entry.get("name") and not entry.get("scope"):
                continue
            # 按「口径」去重：同一个主体换个日期/病害是两条不同的口径，不能互相顶掉。
            key = pool_key(entry)
            pool = [item for item in pool if pool_key(item) != key]
            pool.insert(0, entry)
        merged["subject_pool"] = pool[:5]
    set_memory(_session_key(session_id), merged)


def subject_pool(session_id: str | None) -> list[dict[str, Any]]:
    """最近几轮出现过的「主体 → 数值」（最新的在前）。"""
    context = load_session(session_id)
    if _context_expired(context):
        return []
    return list(context.get("subject_pool") or [])


def result_subject(result: dict[str, Any]) -> dict[str, Any] | None:
    """从本轮结果里认出"这批记录是谁"（记录编号），供下一轮追问它的字段。"""
    rows = result.get("rows") or []
    numbers = [
        str(row.get("problem_number"))
        for row in rows[:200]
        if str(row.get("problem_number") or "").strip()
    ]
    if not numbers:
        return None
    return {"problem_numbers": sorted(set(numbers))[:100]}


# 结果里可能出现的"具体对象 id"：用户下一轮说"那次/那个"时，指的就是它
_ENTITY_ID_FIELDS = (
    "task_number",
    "patrol_number",
    "patrol_id",
    "problem_number",
    "problem_id",
    "device_code",
    "id",
)
# 多行结果里最多记多少个 id：并列/前几名一般几条；几百行的明细清单不算"那次"。
_ENTITY_ROWS_LIMIT = 20


def result_entity(result: dict[str, Any]) -> dict[str, Any] | None:
    """本轮结果是**唯一一条**（"最多的那次""最高的那条"）时，记住它的 id。

    见 docs/长对话智能化升级完整方案-给codex.md Bug 6：
    R2 算出"最多的那次巡检 ID=xxx"，R3 说"把那次巡检的明细调出来"时要能按这个 id 过滤，
    否则只继承路线+时间，查出来是整个范围的数据。
    也覆盖 Bug 7："哪条路密度最高" → 记住那条路，下一轮"密度最高的那条路"直接用，
    不用再算一遍密度。
    """
    rows = result.get("rows") or []
    # 并列的情况（shape=top 返回多条并列）：把这几条都记下来，
    # 下一轮说"那次"时按"这几条"过滤（否则只知道一条，答不全）
    if len(rows) > 1:
        # 并列/前几名（"最多的那次巡检"有 8 次并列）也要记住这组 id。
        # 以前只认 shape=="top"，而模板出来的并列结果 shape 常是 list/distribution，
        # 于是这一轮什么都没记住，下一轮"把那次调出来"就丢了条件 → 查成全量。
        # 只认**编号类列**，并且行数少（并列/前几名）；几百行的明细清单不是"那次"，
        # 记下来反而会把几百个 id 塞进过滤条件。
        if len(rows) <= _ENTITY_ROWS_LIMIT:
            column = next(
                (
                    name
                    for name in rows[0]
                    if name in _ENTITY_ID_FIELDS and str(rows[0].get(name) or "").strip()
                ),
                None,
            )
            if column:
                ids = [
                    str(row.get(column)).strip()
                    for row in rows
                    if str(row.get(column) or "").strip()
                ]
                ids = list(dict.fromkeys(ids))
                if ids:
                    return {
                        "field": column,
                        "id": ids[0],
                        "ids": ids,
                        "fields": [column],
                        "summary": str(result.get("reply") or "")[:120],
                    }
        return None
    if len(rows) != 1:
        return None
    row = rows[0]
    for field in _ENTITY_ID_FIELDS:
        value = str(row.get(field) or "").strip()
        if value and value.lower() not in {"null", "none", "0"}:
            # 同一个 id 可能出现在多列里（patrol_id 和 patrol_number 常是同一个值），
            # 都记下来——查询前用一次探测挑出真能对上数据的那一列（否则过滤到错列 → 0 条）
            same_value_fields = [
                name for name, cell in row.items() if str(cell or "").strip() == value
            ] or [field]
            return {
                "field": field,
                "id": value,
                "fields": same_value_fields,
                "summary": str(result.get("reply") or "")[:120],
            }
    # 没有 id 列时，退一步：唯一一行里"唯一的那个文字值"就是上一轮找到的对象
    # （例如密度最高那条路的结果行是 type=宝安大道西乡段 + 密度值）。
    # 字段名照原样带上，由补全器结合对话判断它是路线名还是病害类型。
    text_values = [
        (column, str(value).strip())
        for column, value in row.items()
        if value not in (None, "")
        and not _is_number_like(str(value))
        and str(value).strip().lower() not in {"null", "none"}
    ]
    if len(text_values) == 1:
        column, value = text_values[0]
        return {
            "field": column,
            "id": value,
            "fields": [column],
            "summary": str(result.get("reply") or "")[:120],
        }
    return None


def _is_number_like(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def last_subject(session_id: str | None) -> dict[str, Any]:
    context = load_session(session_id)
    if _context_expired(context):
        return {}
    value = context.get("last_subject")
    return value if isinstance(value, dict) else {}


# 池子条目按「口径」区分：同一个主体换个日期/病害，就是另一条口径，不能互相顶掉。
_POOL_DIMS = ("route", "time", "damage", "pile", "device")


def _pool_time_key(slots: dict[str, Any]) -> str:
    """时间口径的规范键：单日存日期，跨日存「起~止」。"""
    start, end = slots.get("time_start"), slots.get("time_end")
    if not start or not end:
        return ""
    try:
        first = datetime.fromisoformat(str(start))
        last = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return f"{start}~{end}"
    if (last - first).days == 1:
        return first.date().isoformat()
    return f"{first.date().isoformat()}~{last.date().isoformat()}"


def _pool_time_label(value: str) -> str:
    """把时间口径键写成「7月29日」「7月」这种人话。"""
    if not value:
        return ""
    if "~" not in value:
        try:
            day = datetime.fromisoformat(value)
        except ValueError:
            return value
        return f"{day.month}月{day.day}日"
    start, _, end = value.partition("~")
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return value
    # 整月说成「7月」：闭区间 7月1日~7月31日，或半开区间 7月1日~8月1日
    if first.day == 1 and (last.day == 1 or (last + timedelta(days=1)).day == 1):
        return f"{first.month}月"
    return f"{first.month}月{first.day}日~{last.month}月{last.day}日"


def result_day_window(result: dict[str, Any]) -> tuple[str, str] | None:
    """结果里只出现一个日期时，把它当成本轮的时间口径（"那批病害有多少条？"要用）。"""
    rows = result.get("rows") or []
    if not rows:
        return None
    days: set[date] = set()
    for row in rows:
        for value in row.values():
            if isinstance(value, datetime):
                days.add(value.date())
                continue
            match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
            if match:
                try:
                    days.add(date(int(match[1]), int(match[2]), int(match[3])))
                except ValueError:
                    continue
    if len(days) != 1:
        return None
    first = datetime.combine(next(iter(days)), datetime.min.time())
    return (
        first.strftime("%Y-%m-%d %H:%M:%S"),
        (first + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _pool_damage(slots: dict[str, Any]) -> str:
    if slots.get("damage_matches"):
        return "、".join(item["problem_type"] for item in slots["damage_matches"])
    return str(slots.get("damage_type") or "")


def pool_scope(plan: dict[str, Any]) -> dict[str, str]:
    """本条查询的口径：路线 / 时间 / 病害 / 桩号 / 设备（空的不放）。"""
    slots = plan.get("slots", {})
    route = slots.get("route_match") or {}
    scope: dict[str, str] = {}
    route_name = str(
        slots.get("route") or route.get("short_name") or route.get("full_name") or ""
    )
    if route_name:
        scope["route"] = route_name
    time_key = _pool_time_key(slots)
    if time_key:
        scope["time"] = time_key
    damage = _pool_damage(slots)
    if damage:
        scope["damage"] = damage
    if slots.get("pile_start") is not None and slots.get("pile_end") is not None:
        scope["pile"] = f"{int(slots['pile_start'])}~{int(slots['pile_end'])}"
    if slots.get("device_id"):
        scope["device"] = str(slots["device_id"])
    return scope


def pool_key(entry: dict[str, Any]) -> str:
    """条目身份 = 主体名 + 完整口径。

    必须带主体名：分组结果（"两条路各多少"）里每条的 scope 是**相同的共有口径**
    （按主体分组后那一维被摘掉了），只比 scope 会把两条路当成一条互相覆盖
    ——长对话报告场景8 的"哪条路更多"就因此没有可比较对象。
    """
    scope = entry.get("scope")
    name = str(entry.get("name") or "")
    if not scope:
        return f"name:{name}|damage:{entry.get('damage') or ''}"
    parts = [f"{dim}={scope.get(dim, '')}" for dim in _POOL_DIMS]
    if scope.get("damage"):
        parts.append(f"damage={scope['damage']}")
    return f"name:{name}|" + "|".join(parts)


def pool_label(entry: dict[str, Any], dimension: str = "") -> str:
    """条目在某个维度上的显示名（时间/桩号转成人话）。"""
    scope = entry.get("scope") or {}
    value = str(scope.get(dimension) or "") if dimension else ""
    if dimension == "time":
        return _pool_time_label(value) or str(entry.get("name") or "")
    if dimension == "pile" and value:
        start, _, end = value.partition("~")
        try:
            return f"K{int(start) // 1000}+{int(start) % 1000:03d}~K{int(end) // 1000}+{int(end) % 1000:03d}"
        except ValueError:
            return value
    return value or str(entry.get("name") or "")


def _context_expired(context: dict[str, Any]) -> bool:
    """隔太久再问，视为新话题，不继承上一轮上下文。"""
    stamp = context.get("updated_at")
    if not stamp:
        return False
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    return datetime.now() - moment > timedelta(minutes=settings.session_context_ttl_minutes)


def set_last_question(session_id: str | None, question: str) -> None:
    """记住本会话最后一条问题，供下一句「纠正」时把上下文接回来。"""
    if not session_id or not question:
        return
    set_memory(
        f"{_session_key(session_id)}:last",
        {"question": question, "at": datetime.now().isoformat(timespec="seconds")},
    )


def last_question(session_id: str | None) -> str | None:
    if not session_id:
        return None
    value = get_memory(f"{_session_key(session_id)}:last")
    if not isinstance(value, dict):
        return None
    stamp = value.get("at")
    try:
        if stamp and datetime.now() - datetime.fromisoformat(str(stamp)) > timedelta(
            minutes=settings.session_context_ttl_minutes
        ):
            return None
    except ValueError:
        pass
    question = value.get("question")
    return str(question) if question else None


def set_pending_question(session_id: str | None, question: str) -> None:
    if not session_id or not question:
        return
    set_memory(f"{_session_key(session_id)}:pending", {"question": question})


def pop_pending_question(session_id: str | None) -> str | None:
    if not session_id:
        return None
    key = f"{_session_key(session_id)}:pending"
    value = get_memory(key)
    if not isinstance(value, dict):
        return None
    set_memory(key, {})
    question = value.get("question")
    return str(question) if question else None


def clear_session(session_id: str | None) -> None:
    """彻底清掉某个会话的记忆，用于「新对话」时避免旧上下文污染。"""
    if not session_id:
        return
    delete_memory(_session_key(session_id))
    delete_memory(f"{_session_key(session_id)}:pending")
    delete_memory(f"{_session_key(session_id)}:last")


def contextualize(
    message: str,
    session_id: str | None,
) -> tuple[str, list[str]]:
    """沿用上一条条件。返回 (拼好的问题, 沿用了哪些条件)。"""
    context = load_session(session_id)
    if context and _context_expired(context):
        return message, []
    if re.search(r"具体(?:的)?数据|明细|调出来|列出来|继续|接着|详情", message):
        previous = last_question(session_id)
        if previous:
            return (
                previous.rstrip("。；; ")
                + "；"
                + message.strip()
                + "（沿用上一问的筛选条件和查询口径）",
                [previous],
            )
    if not context:
        return message, []

    # ── 补全交给 LLM（docs/项目智能化升级方案.md：判断权从规则拿给 LLM）──
    # 现在只要这个会话有历史，就先问 LLM："这句话是新话题、还是继承/替换/加条件？"
    # ——它理解不了就原样返回，不在代码里补关键词规则。
    understood = _llm_complete_followup(message, session_id)
    if not understood:
        previous = last_question(session_id)
        if previous and re.search(
            r"具体(?:的)?数据|明细|调出来|列出来|继续|接着|详情",
            message,
        ):
            return (
                previous.rstrip("。；; ")
                + "；"
                + message.strip()
                + "（沿用上一问的筛选条件和查询口径）",
                [previous],
            )
        return message, []
    rewritten = str(understood.get("question") or "").strip()
    if understood.get("relation") == "new_topic":
        clear_session(session_id)
        if understood.get("objects"):
            save_session(session_id, {"last_objects": understood["objects"]})
        return message, []
    if not rewritten:
        return message, []
    if understood.get("objects"):
        save_session(session_id, {"last_objects": understood["objects"]})
    return rewritten, [] if rewritten == message else [rewritten]


def contextualize_message(message: str, session_id: str | None) -> str:
    return contextualize(message, session_id)[0]


def context_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    slots = plan.get("slots", {})
    route = slots.get("route_match") or {}
    return {
        "route": slots.get("route") or route.get("short_name") or route.get("full_name"),
        "route_id": route.get("id"),
        "pile_start": slots.get("pile_start"),
        "pile_end": slots.get("pile_end"),
        "damage_type": slots.get("damage_type"),
        "device_id": slots.get("device_id"),
        "time_start": slots.get("time_start"),
        "time_end": slots.get("time_end"),
        "kind": plan.get("kind"),
        "shape": plan.get("shape"),
    }
