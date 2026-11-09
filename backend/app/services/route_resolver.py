"""路线 / 病害类型的解析与匹配。

从 `nl2sql.py` 拆出（docs/代码走查与目录结构重构方案.md 第三节），**纯搬移**：
函数体一行没改，`nl2sql.py` 里按原名 re-export，外部 `nl2sql._resolve_route(...)`
这类调用不受影响。

依赖很干净：只用 `db`（连库）与 `logging_service`（留痕），不 import nl2sql，
所以不会循环依赖。`QueryPlanError` 跟着搬到这里（它是路线解析抛的主力异常），
nl2sql 里同样 re-export，`except QueryPlanError` / `nl2sql.QueryPlanError` 继续有效。
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

from .db import db_manager
from ..config import settings
from . import text_match
from .logging_service import log_error
from .resource_loader import load_capability_file


class QueryPlanError(RuntimeError):
    pass


# 路线台账表：路线条件要落到哪一列、要经哪几张表中转，全部由字段目录的关系链推导
_ROUTE_TABLE = "basic_facilities_routes"


def _known_damage_types() -> list[dict[str, str]]:
    from app.query.capability_registry import CapabilityRegistry, list_entities

    registry = CapabilityRegistry.load()
    return [
        {
            "problem_code": item.code,
            "problem_type": item.label,
            "problem_alias": "、".join(item.aliases),
        }
        for item in list_entities(registry, "disease_type")
    ]


def _resolve_route(route_text: str) -> dict[str, Any]:
    route = route_text.replace("'", "''")
    # 在**已缓存的路线台账**上做匹配（以前每次都在库里 LIKE 一遍，0.3~0.5 秒/次）
    lowered = route_text.strip().lower()
    rows = [
        row
        for row in _route_rows()
        if any(
            lowered in str(row.get(key) or "").lower()
            for key in ("code", "short_name", "full_name")
        )
    ][:20]
    if not rows:
        # 别名归一：用户按口语加"高速/高速公路/公路"后缀（"包头-茂名高速"），
        # 台账里往往就是不带的短名（"包头-茂名"）；连字符全角/半角也常混用。
        # 归一后**唯一**命中才认，命中多条仍然走"请确认是哪一条"。
        rows = _alias_route_rows(route_text)
    if not rows:
        # 用前两个字模糊找相近路线（"宝安大道龙华段" → 含"宝安"的两条路），
        # 比丢一句"可用路线示例"有用得多。
        probe = _clean_route_name(route_text)[:2]
        safe_probe = probe.replace("'", "''")
        similar: list[str] = []
        if probe:
            similar = [
                str(item.get("short_name") or item.get("full_name"))
                for item in db_manager.query(
                    "mysql",
                    "SELECT DISTINCT short_name, full_name FROM basic_facilities_routes "
                    f"WHERE del_flag = 0 AND (short_name LIKE '%{safe_probe}%' OR full_name LIKE '%{safe_probe}%') "
                    "LIMIT 5",
                )["rows"]
                if item.get("short_name") or item.get("full_name")
            ]
        if similar:
            raise QueryPlanError(
                f"当前路线台账没有找到与“{route_text}”唯一匹配的路线。"
                f"可能对应的路线有：{'、'.join(similar)}。"
                "确认前无法可靠统计，请选择其中一条或补充路线编码。"
            )
        raise QueryPlanError(
            f"当前路线台账没有收录“{route_text}”，"
            "因此无法可靠统计该路线的数据。"
            "请补充台账中已登记的完整路线名称或编码。"
        )

    exact = [
        row
        for row in rows
        if route_text in {row.get("code"), row.get("short_name"), row.get("full_name")}
    ]
    candidates = exact or rows
    candidates = list({str(row.get("id")): row for row in candidates}.values())
    if len(candidates) > 1:
        ranked = text_match.rank_rows(
            route_text,
            candidates,
            ("short_name", "full_name", "code"),
        )
        best = text_match.unique_best(ranked)
        if best:
            return {key: value for key, value in best.items() if key not in {"score", "matched_name"}}
        names = [item.get("matched_name") or item.get("short_name") or item.get("id") for item in ranked]
        raise QueryPlanError(f"路线“{route_text}”匹配到多条记录：{', '.join(names[:5])}，请补充更精确的路线名称。")
    return candidates[0]


def _resolve_damage_type(damage_text: str) -> list[dict[str, str]]:
    from app.query.capability_registry import CapabilityRegistry, resolve_entity

    text = str(damage_text or "").strip()
    registry = CapabilityRegistry.load()
    resolution = resolve_entity(registry, "disease_type", text)
    if resolution.status == "ambiguous":
        names = "、".join(item.label for item in resolution.values[:8])
        raise QueryPlanError(
            f"病害类型“{text}”匹配到多个值：{names}，请补充具体名称。"
        )
    if resolution.status == "unsupported" or not resolution.values:
        raise QueryPlanError(
            f"没有识别到病害类型“{damage_text}”，请使用病害字典里的标准名称。"
        )
    return [
        {
            "problem_code": item.code,
            "problem_type": item.label,
            "problem_alias": "、".join(item.aliases),
        }
        for item in resolution.values
    ]


_GENERIC_ROUTE_WORDS = (
    "路段",
    "路线",
    "线路",
    "道路",
    "公路",
    "路面",
    "高速",
    "沿线",
    "干线",
    "支线",
    "这条路",
    "哪条路",
    "这条",
    "一条",
)


# 疑问词：出现在候选路线名里就说明那不是一条路的名字（"西乡段哪个路格"）
_QUESTION_WORDS = (
    "哪个", "哪些", "哪里", "多少", "什么", "怎么", "是否", "几",
    # 动词/清单词：非贪婪匹配会把"把路线台账明细全部调出来"里的"调出路"当成路线名
    # （实测报"没有找到路线'调出路'"）
    "调出", "列出", "查看", "查询", "显示", "给我", "全部", "所有", "明细", "台账", "记录", "完整字段",
    # 代词：非贪婪匹配会把"它的路线名称"里的"它的路"当成路线名（实测直接报"没有找到路线它的路"）
    "它的", "他的", "它所在", "它", "该", "此", "这", "那",
)


def _unmatched_route_token(message: str) -> str | None:
    """句子里像路线名但没匹配到记录的词（用于"没有找到 X，相近的有…"）。"""
    # 只在句首/标点后取词，避免把"…不属于西乡段"整段当成路线名。
    # 结尾的"路"后面跟着 况/面/灯/标/肩/口/基/网 时是复合名词（路况/路面/路灯…），不是路线名：
    # 实测"片区路况横向对比"被抽成路线名"片区路"，然后报"没有找到路线片区路"。
    for token in re.findall(
        r"(?:^|[，。；？\s])([\u4e00-\u9fa5]{2,10}?(?:段|线|路))(?!况|面|灯|标|肩|口|基|网|缘|堤|障|格|段)",
        message,
    ):
        # 泛称（"分路线""每条路""哪条路""这条路""沿线"…）不是路线名，不能当候选去解析。
        if token in _GENERIC_ROUTE_WORDS:
            continue
        # 候选里带疑问词（"西乡段哪个路"）说明这是问句，不是路线名
        if any(word in token for word in _QUESTION_WORDS):
            continue
        if any(
            generic in token
            for generic in (
                "路线",
                "路段",
                "公路",
                "道路",
                "路面",
                "分路",
                "每条",
                "哪条",
                "这条",
                "那条",
                "这段",
                "那段",
                "哪段",
                "该段",
                "每段",
                "各段",
                "一段",
                "所有",
                "沿线",
                "各条",
                "几条",
                "片区",
            )
        ):
            continue
        if len(token) >= 3:
            # 匹配到的可能是更长词的**前半截**： "包含路线名称" 被切成 "包含路"、
            # "并计算起止桩号" 被切成 "并计算起"，然后去库里找路线 → 报"没有找到路线'包含路'"。
            # 判据：token 末尾那个字 + 紧跟的后一个字，合起来是不是"路线/路段/线路"这类
            # 复合词（"西乡段泛油…"这种正常路线名后面跟的是"泛"，不受影响）。
            end = message.find(token)
            following = message[end + len(token) : end + len(token) + 1] if end >= 0 else ""
            if following and (token[-1] + following) in (
                "路线",
                "路段",
                "线路",
                "路面",
                "路况",
                "路基",
                "路肩",
                "起点",
                "起止",
                "起终",
            ):
                continue
            return token
    return None


def _route_text_in_message(message: str) -> str | None:
    matches = _route_matches(message)
    if not matches:
        return None
    row = matches[0]
    return str(row.get("short_name") or row.get("full_name") or row.get("code") or "")


def _route_pieces(message: str, name: str, min_len: int = 2, max_len: int = 6) -> list[str]:
    """路线名里有哪一段出现在消息里（用户经常只写一部分，例如「西乡段」）。"""
    if len(name) < min_len:
        return []
    pieces: list[str] = []
    for size in range(min(max_len, len(name)), min_len - 1, -1):
        for start in range(0, len(name) - size + 1):
            piece = name[start : start + size]
            if piece in message:
                pieces.append(piece)
    return pieces


def _clean_route_name(text: str) -> str:
    """路线名去标点（"宝安中心--恒明珠" ↔ "宝安中心恒明珠段" 要能比得上）。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]", "", str(text or ""))


def _route_name_rules() -> dict[str, tuple[str, ...]]:
    aliases = load_capability_file("aliases.yaml", {}) or {}
    raw = (
        aliases.get("route_name_rules", {})
        if isinstance(aliases, dict)
        else {}
    )
    if not isinstance(raw, dict):
        raw = {}
    return {
        key: tuple(
            str(item)
            for item in raw.get(key) or ()
            if str(item).strip()
        )
        for key in (
            "connectors",
            "leading_words",
            "suffixes",
            "determiners",
        )
    }


def _route_alias_key(text: str) -> str:
    """路线别名归一：NFKC + 去标点 + 去类型后缀 + 去连接词（"长沙到芷江" → "长沙芷江"）。"""

    key = text_match.normalize(text)
    rules = _route_name_rules()
    for word in sorted(rules["determiners"], key=len, reverse=True):
        key = key.replace(word, "")
    for suffix in sorted(rules["suffixes"], key=len, reverse=True):
        if len(key) > len(suffix) and key.endswith(suffix):
            key = key[: -len(suffix)]
            break
    for word in sorted(rules["connectors"], key=len, reverse=True):
        key = key.replace(word, "")
    for word in sorted(rules["leading_words"], key=len, reverse=True):
        if key.startswith(word):
            key = key[len(word) :]
    return key


def _endpoint_abbreviations(name: str) -> set[str]:
    """由起止地名生成通用简称，例如“平江－洞口高速公路”→“平洞”。"""

    normalized = unicodedata.normalize("NFKC", name)
    parts = [
        re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]", "", item.strip())
        for item in re.split(r"[-—~至到]+", normalized)
        if item.strip()
    ]
    if len(parts) != 2:
        return set()
    suffixes = _route_name_rules()["suffixes"]
    endpoints = []
    for part in parts:
        for suffix in sorted(suffixes, key=len, reverse=True):
            if len(part) > len(suffix) and part.endswith(suffix):
                part = part[: -len(suffix)]
                break
        part = re.sub(r"^(?:[GSXYZ]\d+)", "", part, flags=re.IGNORECASE)
        if len(part) < 2 or not re.match(r"[\u4e00-\u9fa5]", part):
            return set()
        endpoints.append(part)
    return {"".join(item[0] for item in endpoints)}


def _alias_route_rows(route_text: str) -> list[dict[str, Any]]:
    """归一后与用户写法的短名完全相等的路线（唯一命中才认）。"""

    key = _route_alias_key(route_text)
    if len(key) < 2:
        return []
    aliases = load_capability_file("aliases.yaml", {}) or {}
    route_aliases = (
        aliases.get("route_name", {})
        if isinstance(aliases, dict)
        else {}
    )

    def names(row: dict[str, Any]) -> list[str]:
        values = [
            str(row.get(field) or "")
            for field in ("short_name", "full_name", "code")
        ]
        if isinstance(route_aliases, dict):
            for official in ("short_name", "full_name", "code"):
                configured = route_aliases.get(str(row.get(official) or ""))
                if isinstance(configured, str):
                    values.append(configured)
                elif isinstance(configured, (list, tuple)):
                    values.extend(str(item) for item in configured)
        for official in ("short_name", "full_name"):
            values.extend(_endpoint_abbreviations(str(row.get(official) or "")))
        return values

    return [
        row
        for row in _route_rows()
        if any(
            _route_alias_key(name) == key
            for name in names(row)
        )
    ][:20]


# 桩号区间（K10+000~K11+000 / K10+000 到 K11+000）——**不是路线名证据**。
# 路线台账里 3623 条县/乡/村道，代码里带 421321001 这种行政编码；桩号里的 "000"/"100"
# 这类片段会和路线代码/名称撞上，凭空多认出一条路线（实测"西乡段K10+000~K11+000"多出
# 一条 G353宁德-福贡），于是 plan 被当成"多路线对比"、路线过滤整条丢掉。
_PILE_SPAN_RE = re.compile(
    r"[Kk]\s*\d+\s*\+\s*\d+(?:\s*[~～\-—至到]\s*[Kk]?\s*\d+\s*\+\s*\d+)?"
)


def _name_match_text(message: str) -> str:
    """做路线名匹配用的文本：先把桩号区间整段抹掉（含中文"到/至"写法）。"""
    return _PILE_SPAN_RE.sub(" ", str(message or ""))


def _usable_piece(piece: str, name: str) -> bool:
    """部分匹配的片段能不能当路线名证据。

    纯数字/字母片段（"100"、"000"、"353"）**一律不认**——它们最容易和桩号、行政编码撞车。
    例外：片段就是这个名字的全部（用户直接说了完整代码 "G353"），那是点名，不是碎片。
    """
    if piece == name:
        return True
    return bool(re.search(r"[\u4e00-\u9fa5]", piece))


def _contains_route_token(text: str, name: str) -> bool:
    """name 在 text 里出现；对**编码类**名字要求整词边界（"G353" 不能命中 "G353421…" 的一部分）。"""
    if not name or name not in text:
        return False
    if re.fullmatch(r"[0-9A-Za-z_\-]+", name):
        return re.search(
            rf"(?<![0-9A-Za-z_]){re.escape(name)}(?![0-9A-Za-z_])", text
        ) is not None
    return True


def _suspicious_piece(message: str, piece: str, row: dict[str, Any]) -> bool:
    """模糊匹配的误伤防护："宝安大道龙华段"里能匹配到"宝安大道"（西乡段的前缀），
    但问题里出现了"…龙华段"这种不属于任何路线名的路段名 → 判定可疑，别认这条。

    只对"匹配片段后面还跟着别的字"的情况警觉：
      "宝安大道龙华段" 里匹配到"宝安大道"，后面跟的是"龙华段"（不属于这条路）→ 可疑
      "…不属于西乡段" 里"西乡段"就在末尾 → 不可疑
    """
    name_like = [
        token
        # 非贪婪：只取"最短的像路线名的前缀"。贪婪版会把"西乡段病害密度比其他路"整段吃进去，
        # 于是"西乡段"被当成"另一条路的名字"而否掉（实测这句直接报"没找到路线"）。
        # 高速/公路也算路线名后缀：用户说"广深高速"时，那是一个独立的路线名，
        # 不能拿它的尾巴"深高速"去认"武汉－深圳（G0422武深高速）"。
        for token in re.findall(
            r"[\u4e00-\u9fa5]{2,20}?(?:高速|公路|段|线|路)",
            message,
        )
        # "西乡段哪个路格"这种：token 从路线名一直连到疑问词，不是"另一条路的名字"。
        if not any(word in token for word in _QUESTION_WORDS)
    ]
    route_names = [
        _clean_route_name(row.get(key))
        for key in ("short_name", "full_name", "code")
    ]
    for token in name_like:
        if piece not in token:
            continue
        start = token.index(piece)
        before = token[:start]
        after = token[start + len(piece) :]
        # 片段只是"另一个更像独立路线名的词"的后半截时同样可疑：
        # "广深高速"里的"深高速"不是"武深高速"的证据。
        head = re.sub(r"(高速|公路|段|线|路)$", "", before)
        if head:
            combined = _clean_route_name(head + piece)
            if not any(
                combined in name or name in combined for name in route_names
            ):
                return True
        # 后面跟的是"这两条路/那条路/附近"这类泛指时不算可疑，
        # 只有像"龙华段"这种具体地名才算（只多一个"段/线/路"字的是正常叫法）。
        if after and after[0] in "这那的两种三个几和与等近内上中哪":
            continue
        # 称呼后缀（"…段/线/路"）不算"另一条路"：用户把"宝安中心--恒明珠"写成
        # "宝安中心恒明珠段"是正常叫法，不能因为多一个"段"字就判可疑。
        core = re.sub(r"^(段|线|路)", "", after)
        if not core:
            continue
        combined = _clean_route_name(piece + core)
        if len(core) >= 1 and not any(combined in name or name in combined for name in route_names):
            return True
    return False


def _partial_candidates(
    message: str, rows: list[dict[str, Any]], exclude: set[str], min_len: int = 2
) -> list[tuple[str, dict[str, Any]]]:
    """用户只写了路线名一部分时的候选（"恒明珠段"→"宝安中心--恒明珠"）。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        if str(row.get("id")) in exclude:
            continue
        best = ""
        # code 不参与**部分**匹配：用户写半截代码（"G353"）不当证据；完整代码走整词精确匹配。
        for key in ("short_name", "full_name"):
            name = str(row.get(key) or "").strip()
            for piece in _route_pieces(message, name):
                if (
                    len(piece) >= min_len
                    and len(piece) > len(best)
                    and _usable_piece(piece, name)
                ):
                    best = piece
        if best:
            out.append((best, row))
    return out


def _partial_candidates_unique(
    message: str,
    rows: list[dict[str, Any]],
    exclude: set[str],
    min_len: int = 3,
    max_len: int = 6,
) -> list[tuple[str, dict[str, Any]]]:
    """补充部分命中，且片段必须**只属于这一条路线**（"恒明珠"唯一；"宝安大道"一堆都有 → 不要）。

    一次扫描同时算出"候选片段"和"每个片段被几条路线包含"——按条逐个去反问所有路线名
    是 O(件数×条数)，3600 条路线实测要 35 秒（第一版就踩了这个坑）。
    """
    owners: dict[str, set[str]] = {}
    best_by_row: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        row_id = str(row.get("id"))
        if row_id in exclude:
            continue
        best = ""
        for key in ("short_name", "full_name"):
            name = str(row.get(key) or "").strip()
            for size in range(min_len, min(max_len, len(name)) + 1):
                for start in range(0, len(name) - size + 1):
                    piece = name[start : start + size]
                    if piece not in message:
                        continue
                    # 纯数字/字母片段不当证据（"100" 这类会和桩号/行政编码撞车）
                    if not _usable_piece(piece, name):
                        continue
                    owners.setdefault(piece, set()).add(row_id)
                    if len(piece) > len(best):
                        best = piece
        if best:
            best_by_row.append((best, row))
    out: list[tuple[str, dict[str, Any]]] = []
    for piece, row in best_by_row:
        if owners.get(piece) != {str(row.get("id"))}:
            continue  # 这个片段好几条路线都有 → 认不准，不要
        if _suspicious_piece(message, piece, row):
            continue
        out.append((piece, row))
    return out


def _route_section_count(route_id: str) -> int:
    """这条路线台账里有几个路段？（用来判断"按路段分组"有没有意义）

    实测西乡段整条路只挂了**一个**路段（K0+000~K5+100），
    问"按具体路段分组"模型只好自己把整段再切碎，答出 100 组、每组的密度都算错。
    """
    if not route_id:
        return 0
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT COUNT(*) AS c FROM basic_facilities_sections "
            f"WHERE del_flag = 0 AND route_id = '{str(route_id).replace(chr(39), chr(39) * 2)}'",
        )["rows"]
    except Exception:  # noqa: BLE001
        return 0
    try:
        return int(rows[0].get("c") or 0)
    except (TypeError, ValueError, IndexError):
        return 0


def _route_matches(message: str) -> list[dict[str, Any]]:
    """找出消息里提到的所有路线（按 id 去重，去掉被更长名称包含的短名）。"""
    rows = _route_rows()
    return _route_matches_in(message, rows)


_ROUTE_ROWS: tuple[float, list[dict[str, Any]]] | None = None
_ROUTE_ROWS_TTL = settings.reference_data_ttl_seconds


def _route_rows() -> list[dict[str, Any]]:
    """路线台账（id/名称）带 5 分钟缓存：3623 条、库还在 SSH 隧道后面，
    以前**每次规划都全量拉一遍**（实测 0.3~0.9 秒），是纯重复开销。
    新增路线最迟 5 分钟就能被识别；要立即生效重启一次即可。"""
    global _ROUTE_ROWS
    now = time.time()
    if _ROUTE_ROWS and now - _ROUTE_ROWS[0] < _ROUTE_ROWS_TTL:
        return _ROUTE_ROWS[1]
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT id, code, short_name, full_name FROM basic_facilities_routes WHERE del_flag = 0",
        )["rows"]
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "route_rows_failed", exc)
        if _ROUTE_ROWS:
            # 有旧缓存（哪怕已经过期）就接着用：正在跑的这条问题不该因为一次抖动就查不到路线
            return _ROUTE_ROWS[1]
        # 没旧缓存就别写缓存：一次失败不该把"没有路线"钉死 5 分钟——让下次请求重新试
        return []
    _ROUTE_ROWS = (now, rows)
    return rows


def _route_matches_in(message: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 匹配前的文本：桩号区间整段抹掉（那是"位置条件"，不是路线名证据）。
    text = _name_match_text(message)
    hits: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        names = [
            str(row.get(key) or "").strip()
            for key in ("code", "short_name", "full_name")
        ]
        # 编码类名字（G353 / X037…）要求**整词**命中：不能拿它的一部分当证据
        matched = [name for name in names if name and _contains_route_token(text, name)]
        if matched:
            hits.append((max(matched, key=len), row))

    if not hits:
        # 退化匹配：用户只写了路线名的一部分（「西乡段」→ 宝安大道西乡段），
        # 之前这种情况会静默丢掉路线过滤，答案变成全库。
        partial = _partial_candidates(text, rows, exclude=set())
        if partial:
            # ⚠️ 不能"跨行取全局最长片段"：两条路都只写简称时，长一点的那条会把另一条顶掉
            # （实测"西乡段和恒明珠段7月各多少"只剩恒明珠段）。每条路取自己的 best 片段。
            survived = [
                (piece, row)
                for piece, row in partial
                if not _suspicious_piece(text, piece, row)
            ]
            strong = [(piece, row) for piece, row in survived if len(piece) >= 3]
            if strong:
                hits = strong
            else:
                # 两字片段太容易撞（例如"宝安"）：只在**唯一命中 + 短追问**时才认
                # （"那西乡呢？"），长句子里的两字巧合一律不认。
                weak = [(piece, row) for piece, row in survived if len(piece) == 2]
                ids = {str(row.get("id")) for _, row in weak}
                hits = weak if len(ids) == 1 and len(text.strip()) <= 12 else []
    else:
        # 一句话里一条写全名、另一条写简称是常态：
        # "宝安大道西乡段…宝安中心恒明珠段…" —— 以前只在"一条精确命中都没有"时才做部分匹配，
        # 于是只认出写全名的西乡，两条路对比直接退化成单条（用户实测）。
        # 现在在精确命中之外**补充**部分命中，片段要求 ≥3 字（两字太容易撞）。
        have = {str(item[1].get("id")) for item in hits}
        hits.extend(_partial_candidates_unique(text, rows, exclude=have))

    # ── 多路线准入（带桩号区间时）────────────────────────────────────────────
    # 桩号/编码片段最容易撞出假路线（实测 K10+000~K11+000 凭空多出一条 G353）。
    # 所以只要句子里有桩号区间，就只认**靠中文名称命中**的路线：纯代码/纯数字命中一律丢掉；
    # 丢掉后如果只剩一条，自然就是"单路线 + 桩号条件"（不会再把路线过滤丢掉）。
    if hits and _PILE_SPAN_RE.search(str(message)):
        credible = [(name, row) for name, row in hits if re.search(r"[\u4e00-\u9fa5]", str(name))]
        if credible:
            hits = credible

    hits.sort(key=lambda item: len(item[0]), reverse=True)
    kept: list[tuple[str, dict[str, Any]]] = []
    for name, row in hits:
        if any(name != other and name in other for other, _ in kept):
            continue
        if any(str(row["id"]) == str(existing["id"]) for _, existing in kept):
            continue
        kept.append((name, row))
    # 按**在问题里出现的先后**返回，而不是按路线台账的行序：多路线示例回填是按序号填
    # `{route_id}` / `{route_id_2}` 的，库里行序和用户说话顺序经常不一样 → 参数填反
    # （实测 pair-3f50df81…："算平均面积的路线"和"被比较的路线"对调，标签和数据完全对不上）。
    kept.sort(key=lambda item: (text.find(item[0]) if text.find(item[0]) >= 0 else 10**9))
    return [row for _, row in kept]
