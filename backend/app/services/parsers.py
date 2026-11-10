from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


def _to_km(value: str) -> int:
    text = value.strip().lower().replace("k", "")
    if "+" in text:
        km, meter = text.split("+", 1)
        return int(km) * 1000 + int(meter)
    return int(text) * 1000


def parse_pile_range(message: str) -> tuple[int, int] | None:
    # 不能用 \b：中文（"K1+000至K3+000"里的"至"）在 Python 里算 word 字符，
    # 会导致后面那个桩号匹配不到，整段区间被截断成单点。
    pattern = re.compile(r"(?<![A-Za-z0-9])[kK]\s*(\d{1,5})(?:\s*\+\s*(\d{1,3}))?(?![0-9])")
    matches = pattern.findall(message)
    values: list[int] = []
    for km, meter in matches:
        values.append(int(km) * 1000 + (int(meter) if meter else 0))
    if len(values) >= 2:
        start, end = values[0], values[1]
        if start > end:
            start, end = end, start
        return start, end
    if len(values) == 1:
        start = values[0]
        # 单桩号时默认向后查 999 米。
        return start, start + 999
    return None


def _to_day_range(year: int, month: int, day: int) -> tuple[datetime, datetime] | None:
    try:
        start = datetime(year, month, day)
    except ValueError:
        return None
    return start, start + timedelta(days=1)


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def parse_days(message: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """把问题里提到的所有「某日」合并成一个连续区间。

    「7月29日和7月30日两天」以前只取第一天，总数直接少一半。
    只有各天连续时才合并（29、30 → [29,31)），不连续就返回 None 交给单日逻辑。
    """
    now = now or datetime.now()
    found: list[datetime] = []
    for year, month, day in re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", message):
        moment = _safe_date(int(year), int(month), int(day))
        if moment:
            found.append(moment)
    for year, month, day in re.findall(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", message):
        moment = _safe_date(int(year), int(month), int(day))
        if moment:
            found.append(moment)
    for month, day in re.findall(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", message):
        moment = _safe_date(now.year, int(month), int(day))
        if moment:
            found.append(moment)
    days = sorted(set(found))
    if not days:
        return None
    if (days[-1] - days[0]).days + 1 != len(days):
        return None
    return days[0], days[-1] + timedelta(days=1)


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime] | None:
    if not 1 <= month <= 12:
        return None
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def parse_month_metrics(
    message: str, now: datetime | None = None
) -> list[tuple[str, datetime, datetime]]:
    """问题里单独提到的每个「X月」（后面不接日/号），用于「6月多少？7月多少？」这种多口径问法。"""
    now = now or datetime.now()
    found: list[tuple[str, datetime, datetime]] = []
    seen: set[tuple[int, int]] = set()
    patterns = (
        (r"(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d)", True),
        (r"(?<!\d)(\d{1,2})\s*月(?!\s*\d)", False),
    )
    for pattern, with_year in patterns:
        for match in re.finditer(pattern, message):
            if with_year:
                year, month = int(match.group(1)), int(match.group(2))
                label = f"{month}月"
            else:
                year, month = now.year, int(match.group(1))
                label = f"{month}月"
            bounds = _month_bounds(year, month)
            if not bounds or (year, month) in seen:
                continue
            seen.add((year, month))
            found.append((label, bounds[0], bounds[1]))
    return found


def parse_day_metrics(
    message: str, now: datetime | None = None
) -> list[tuple[str, datetime, datetime]]:
    """问题里单独提到的每个「X月Y日」（不做连续合并，便于逐个口径输出）。"""
    now = now or datetime.now()
    found: list[tuple[str, datetime, datetime]] = []
    seen: set[datetime] = set()
    patterns = (
        (r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", True),
        (r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", True),
        (r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", False),
    )
    for pattern, with_year in patterns:
        for match in re.finditer(pattern, message):
            if with_year:
                year, month, day = (int(part) for part in match.groups()[:3])
            else:
                year, month, day = now.year, int(match.group(1)), int(match.group(2))
            moment = _safe_date(year, month, day)
            if not moment or moment in seen:
                continue
            seen.add(moment)
            found.append((f"{month}月{day}日", moment, moment + timedelta(days=1)))
    found.sort(key=lambda item: item[1])
    return found


def parse_pile_side(message: str) -> tuple[str, str, str] | None:
    """「K1以西 / K1以东」→ 别名、标签、过滤条件。"""
    match = re.search(r"[Kk](\d+)(?:\+(\d+))?\s*(以[东西])", message)
    if not match:
        return None
    km, meter, direction = int(match.group(1)), match.group(2), match.group(3)
    distance = km * 1000 + int(meter or 0)
    if direction == "以西":
        return ("west_of_k%d" % km, f"K{km}以西", f"p.pile_distance < {distance}")
    return ("east_of_k%d" % km, f"K{km}以东", f"p.pile_distance >= {distance}")


def _month_span(moment: datetime) -> tuple[datetime, datetime]:
    first = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt


def _shift_months(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + (moment.month - 1) + months
    return datetime(index // 12, index % 12 + 1, 1)


def time_range_from_spec(spec: dict[str, Any], now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """**语义层给的时间参照系**（结构化）→ 起止时间。纯算术，不认措辞。

    为什么这样做：靠正则认"第 31 周/这个月第三个星期/上个月第三个星期…"永远认不全，
    而**判断用户说的是哪一类时间**是语义理解（LLM 干），**把类别换算成日期区间**是算术（代码干）
    ——docs/值换算层统一修复方案.md §2.1 的核心思路。词表封闭：

      base   : "now" 或 "YYYY-MM-DD"（参照点，默认 now）
      unit   : day | week | month | quarter | year | iso_week
      offset : 相对参照点的单位偏移（本月=0、上个月=-1、下个月=1）
      nth    : 单位内的第几个（"这个月第三个星期" → 3；不填=整个单位）
      count  : 连续几个单位（"最近两周" → 2）

    例：
      {"unit":"week","offset":0}                    → 本周（周一~下周一）
      {"unit":"month","offset":-1}                  → 上个月
      {"unit":"week","offset":0,"nth":3}            → 这个月第三个星期
      {"unit":"iso_week","nth":31,"base":"2026-01-01"} → 2026 年第 31 周（ISO）
      {"unit":"day","base":"2026-07-20","count":7}  → 2026-07-20 起 7 天
    """
    if not isinstance(spec, dict) or not spec:
        return None
    moment = now or datetime.now()
    base_raw = str(spec.get("base") or "").strip()
    if base_raw and base_raw.lower() != "now":
        parsed = parse_days(base_raw, moment) or parse_month_range(base_raw, moment)
        if parsed:
            moment = parsed[0]
    try:
        offset = int(spec.get("offset") or 0)
        count = int(spec.get("count") or 1)
    except (TypeError, ValueError):
        return None
    nth = spec.get("nth")
    try:
        nth = int(nth) if nth not in (None, "") else None
    except (TypeError, ValueError):
        nth = None
    unit = str(spec.get("unit") or "").strip().lower()
    count = max(count, 1)
    if unit == "day":
        today = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        if not base_raw and count > 1:
            # "最近 N 天"是**往前**数（含今天）：最近 7 天 = 今天往前 7 天
            return today - timedelta(days=count), today
        start = today + timedelta(days=offset)
        return start, start + timedelta(days=count)
    if unit == "week":
        if not base_raw and not nth and count > 1:
            # "最近 N 周"同理往前数（"最近两周"= 今天往前 14 天）
            today = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            return today - timedelta(weeks=count), today
        # 参照点所在周（周一为首日）；nth 表示"该月内第几个星期"
        monday = (moment - timedelta(days=moment.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if nth:
            # 「某月第 N 个星期」：**offset 选月份、nth 选该月内的第几个周一起算的周**
            # （实测"上个月第三个星期"原来错拿当前月的月份去比，算成了这个月的第 3 周）。
            # 约定：以"该周至少有一天落在目标月"为准（本月 1 号所在的那一周算第 1 个）。
            target = _shift_months(moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0), offset)
            first_monday = target - timedelta(days=target.weekday())
            monday = first_monday + timedelta(weeks=nth - 1)
            month_end = _month_span(target)[1]
            if monday >= month_end:  # 数到下一月去了 → 退回目标月最后一个整周
                monday -= timedelta(weeks=1)
            return monday, monday + timedelta(weeks=count)
        monday += timedelta(weeks=offset)
        return monday, monday + timedelta(weeks=count)
    if unit == "month":
        start, _ = _month_span(moment)
        start = _shift_months(start, offset)
        end = _shift_months(start, count)
        return start, end
    if unit == "season":
        # 季节（口语）：春=3-5 月、夏=6-8、秋=9-11、冬=12-2 月（**冬跨年**）。
        # 词表封闭，语义层只要说"哪一季 + 相对偏移"，算术在代码里。
        season_index = {"春": 0, "夏": 1, "秋": 2, "冬": 3}
        name = str(spec.get("season") or "").strip()
        if name in season_index:
            # 季节名已经说清是哪一季 → `offset` 当**年份**偏移用（去年冬天=-1、今年夏天=0）
            start_month = 3 + season_index[name] * 3
            year = moment.year + (start_month - 1) // 12
            if name == "冬" and moment.month in (1, 2) and offset <= 0:
                year -= 1  # 1/2 月说"冬"指正在过的这个冬
            year += offset
        else:
            # 没点名哪一季（"下个季度/这个季节"）→ 按参照点所在季节 + offset（按期数平移）
            index = {3: 0, 4: 0, 5: 0, 6: 1, 7: 1, 8: 1, 9: 2, 10: 2, 11: 2}.get(moment.month, 3)
            index += offset
            start_month = 3 + index * 3
            year = moment.year + (start_month - 1) // 12
        start = datetime(year, (start_month - 1) % 12 + 1, 1)
        return start, _shift_months(start, 3 * count)
    if unit == "quarter":
        first_month = moment.replace(month=((moment.month - 1) // 3) * 3 + 1, day=1)
        start = _shift_months(first_month, offset * 3)
        return start, _shift_months(start, count * 3)
    if unit == "year":
        start = datetime(moment.year + offset, 1, 1)
        return start, datetime(start.year + count, 1, 1)
    if unit == "iso_week":
        week = nth or int(spec.get("week") or 0)
        if not week:
            # 没给周数就是"参照点所在 ISO 周"
            monday = (moment - timedelta(days=moment.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            monday += timedelta(weeks=offset)
            return monday, monday + timedelta(weeks=count)
        year = moment.year
        for candidate in (year, year - 1, year + 1):
            try:
                monday = datetime.fromisocalendar(candidate, week, 1)
            except ValueError:
                continue
            monday += timedelta(weeks=offset)
            return monday, monday + timedelta(weeks=count)
        return None
    return None


_ABS_DAY_PATTERNS = (
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"),
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
)


def _absolute_days(message: str) -> list[datetime]:
    """句子里出现的**明确日期**（升序、去重）。"""
    found: list[datetime] = []
    for pattern in _ABS_DAY_PATTERNS:
        for match in pattern.finditer(message):
            try:
                moment = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            if moment not in found:
                found.append(moment)
    return sorted(found)


def _explicit_day_range(message: str) -> tuple[datetime, datetime] | None:
    """句子里写了**两个及以上明确日期** → 直接当时间范围用（用户的说法最可靠）。

    实测"2026 年第 31 周…时间范围是 2026 年 07 月 20 日（周一）～07 月 26 日（周日），
    把这周相关数据列出来"里那个"**这周**"把明说的范围盖成了当前周（10/12~10/19），
    于是回答"您问的时间段没有数据"（docs/值换算层统一修复方案.md §1.2）。
    只有一天时不算范围——"7月29日有多少处"仍走原来的单日分支。
    """
    days = _absolute_days(message)
    if len(days) < 2:
        return None
    return days[0], days[-1] + timedelta(days=1)


_QUARTER_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}


def _quarter_range(message: str, now: datetime) -> tuple[datetime, datetime] | None:
    """「第 N 季度 / N 季度 / QN」→ 季度区间（左闭右开，正好 3 个月）。

    必须排在**年/月**分支之前：不然后面那条 `(19|20)\\d{2}\\s*年` 会把"2026 年第三季度"
    吃成整年（实测模板预填成了 2026-01-01~2027-01-01）。年份没写就用参照点那一年。
    """
    match = re.search(r"第?\s*([一二三四1234])\s*(?:个)?\s*季度", message) or re.search(
        r"(?i)\bQ([1-4])\b", message
    )
    if not match:
        return None
    quarter = _QUARTER_WORDS.get(match.group(1))
    if not quarter:
        return None
    year_match = re.search(r"((?:19|20)\d{2})", message)
    year = int(year_match.group(1)) if year_match else now.year
    start = datetime(year, (quarter - 1) * 3 + 1, 1)
    return start, _shift_months(start, 3)


def _iso_week_range(message: str, now: datetime) -> tuple[datetime, datetime] | None:
    """「第 N 周」→ ISO 8601 的周一~周日区间（和挖槽/分桶用的 ISO 周一致）。"""
    match = re.search(r"第\s*(\d{1,2})\s*(?:个)?\s*(?:周|星期)", message)
    if not match:
        return None
    week = int(match.group(1))
    if not 1 <= week <= 53:
        return None
    year_match = re.search(r"(\d{4})\s*年", message)
    year = int(year_match.group(1)) if year_match else now.year
    for candidate in (year, year - 1, year + 1):
        try:
            monday = datetime.fromisocalendar(candidate, week, 1)
        except ValueError:
            continue  # 该年没有第 53 周之类
        return monday, monday + timedelta(days=7)
    return None


def parse_month_range(message: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """只解析到「月」的区间，用于算占比时的分母（例如「7 月总数」）。"""
    now = now or datetime.now()
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", message)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
    else:
        bare = re.search(r"(?<!\d)(\d{1,2})\s*月", message)
        if not bare:
            return None
        year, month = now.year, int(bare.group(1))
    if not 1 <= month <= 12:
        return None
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def parse_time_range(message: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    now = now or datetime.now()
    text = message.lower()

    # ① **用户明说的绝对时间范围优先**（docs/值换算层统一修复方案.md §1.2、§阶段0）：
    #    实测"2026 年第 31 周…时间范围是 2026 年 07 月 20 日～07 月 26 日…把这周的数据列出来"
    #    被下面的"这周"分支覆盖成了**当前周**（10/12~10/19），于是回答"您问的时间段没有数据"。
    #    用户写明白的日期是他最可靠的意图，任何相对词（本周/这周/最近）都不该盖过它。
    explicit = _explicit_day_range(message)
    if explicit:
        return explicit
    iso_week = _iso_week_range(message, now)
    if iso_week:
        return iso_week

    if "今天" in text:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if "昨天" in text:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if "最近一个月" in text or "近一个月" in text or "过去一个月" in text:
        return now - timedelta(days=30), now
    if "最近一周" in text or "近一周" in text or "过去一周" in text:
        return now - timedelta(days=7), now
    # 相对时间：上个月/本月/去年/今年/上周/本周（以前只认"最近N天"，这些全静默失败）
    if any(word in text for word in ("上个月", "上月")):
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first - timedelta(days=1)
        return end.replace(day=1), first
    if any(word in text for word in ("本月", "这个月")):
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (first + timedelta(days=32)).replace(day=1)
        return first, next_month
    relative_month = re.search(
        r"(今年|去年)\s*(\d{1,2})\s*月(?!\s*\d)",
        message,
    )
    if relative_month:
        year = now.year if relative_month.group(1) == "今年" else now.year - 1
        bounds = _month_bounds(year, int(relative_month.group(2)))
        if bounds:
            return bounds
    if any(word in text for word in ("去年", "上一年")):
        return datetime(now.year - 1, 1, 1), datetime(now.year, 1, 1)
    if any(word in text for word in ("今年", "本年度")):
        return datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1)
    if any(word in text for word in ("上周", "上个星期")):
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return monday - timedelta(days=7), monday
    if any(word in text for word in ("本周", "这周")):
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return monday, monday + timedelta(days=7)
    if "最近" in text:
        m = re.search(r"最近\s*(\d+)\s*天", text)
        if m:
            days = int(m.group(1))
            return now - timedelta(days=days), now

    # 季度（"2026 年第三季度""三季度""Q3"）：必须排在**年/月**分支之前——否则
    # "2026 年第三季度"会被"2026 年"那条年分支吃成**整年**（实测模板预填成了 2026.1.1~2027.1.1）。
    quarter = _quarter_range(message, now)
    if quarter:
        return quarter

    # 精确到某一天（2026年7月29日 / 2026-07-29 / 7月29日）。必须排在「年月」之前，
    # 否则「2026年7月29日」会被当成整个 7 月。
    days = parse_days(message, now)
    if days:
        return days

    month_day = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", message)
    if month_day:
        found = _to_day_range(now.year, int(month_day.group(1)), int(month_day.group(2)))
        if found:
            return found

    month_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", message)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        return start, end

    # 只写年份（「2024年」「2024 年」）→ 整年区间。必须排在裸月份判断之前。
    year_match = re.search(r"(?<!\d)(\d{4})\s*年(?!\s*\d)", message)
    if year_match:
        year = int(year_match.group(1))
        if 1900 <= year <= 2200:
            return datetime(year, 1, 1), datetime(year + 1, 1, 1)

    # 只说月份（如“7月”）时，按当前年份推断。
    bare_month = re.search(r"(?<!\d)(\d{1,2})\s*月", message)
    if bare_month:
        month = int(bare_month.group(1))
        if 1 <= month <= 12:
            year = now.year
            start = datetime(year, month, 1)
            end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
            return start, end
    return None


# 主体词表：决定「问的是哪张表」。模板只支持 disease，其余主体交给 LLM + 关联链路提示。
SUBJECT_MAIN_TABLE = {
    "disease": "ai_problem",
    "pile_point": "basic_facilities_sections_pile_detail",
    "section": "basic_facilities_sections",
    "patrol": "ai_maintain_patrol",
    "work_order": "ai_work_order",
    # 台账类资产：登记后"桥梁/隧道/边坡/工程"问题的主体与主表一致，
    # 提示词里那句【问题主体：X（主表 Y），不要用病害表作答】才能覆盖到它们
    "bridge": "basic_facilities_bridges",
    "tunnel": "basic_facilities_tunnels",
    "slope": "basic_facilities_slope",
    "project": "project_manage_project_main",
    "route": "basic_facilities_routes",
    "asset": "ai_asset_main",
}

def parse_slots(message: str, time_source: str | None = None) -> dict[str, Any]:
    """预抽结构化槽位；命名实体由语义模型负责，这里只保留桩号和时间。

    `time_source` = "时间说法到哪句话里去找"：
    · 顶层单题不传 —— 就在这句话里找；
    · 复合题的子问题传**本轮完整问题（优化后）**：子问题是拆解器改写的，里面会夹**举例值**
      （"输出周次标签（如'2026年第31周'）"），从改写文本上抽时间等于给用户加了个他根本没问过的
      条件（实测第一步被缩到 7/27~8/3，再叠加"取第2个结果"→ 0 行）。
      注意：优化器补全出来的时间（长对话里继承的"7月"）在完整问题里，属于**合法条件**，照抽。
      例外：这一步的时间是**上一步算出来回填**的（"{{peak_week}} 这一周的明细"），
      那时候调用方会传回这一步的问法本身 —— 那是真实条件，不是编的。
    """
    pile = parse_pile_range(message)
    time_range = parse_time_range(time_source or message)
    return {
        "route": None,
        "pile_start": pile[0] if pile else None,
        "pile_end": pile[1] if pile else None,
        "damage_type": None,
        "time_start": time_range[0].strftime("%Y-%m-%d %H:%M:%S") if time_range else None,
        "time_end": time_range[1].strftime("%Y-%m-%d %H:%M:%S") if time_range else None,
        "up_or_down": None,
    }
