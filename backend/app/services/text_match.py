from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


def normalize(text: Any) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def score_name(query: str, candidate: str) -> float:
    """字符层相似度；不依赖业务词表。"""
    left, right = normalize(query), normalize(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if left in right or right in left:
        coverage = min(len(left), len(right)) / max(len(left), len(right))
        # 部分名称是很强的业务证据：“西乡” vs “宝安大道西乡段”。
        ratio = max(ratio, 0.88 + 0.10 * coverage)
    a, b = _bigrams(left), _bigrams(right)
    jaccard = len(a & b) / len(a | b) if a | b else 0.0
    prefix = 0
    for a_char, b_char in zip(left, right):
        if a_char != b_char:
            break
        prefix += 1
    if prefix:
        ratio = max(ratio, 0.55 + 0.35 * min(prefix / len(left), prefix / len(right)))
    return min(1.0, max(ratio, jaccard))


def rank_rows(
    query: str,
    rows: list[dict[str, Any]],
    name_keys: tuple[str, ...],
    *,
    min_score: float = 0.72,
    ambiguity_gap: float = 0.06,
    limit: int = 10,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in rows:
        best_name, best_score = "", 0.0
        for key in name_keys:
            name = str(row.get(key) or "").strip()
            value = score_name(query, name)
            if value > best_score:
                best_name, best_score = name, value
        if best_score >= min_score:
            scored.append({**row, "matched_name": best_name, "score": round(best_score, 4)})
    scored.sort(key=lambda item: (-float(item["score"]), len(str(item.get("matched_name") or ""))))
    if not scored:
        return []
    top = float(scored[0]["score"])
    close = [item for item in scored if float(item["score"]) >= top - ambiguity_gap]
    return (close or scored[:1])[:limit]


def unique_best(matches: list[dict[str, Any]], ambiguity_gap: float = 0.06) -> dict[str, Any] | None:
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if float(matches[0]["score"]) - float(matches[1]["score"]) >= ambiguity_gap:
        return matches[0]
    return None
