from __future__ import annotations

import re


FILLER_WORDS = (
    "请问",
    "麻烦",
    "帮我",
    "帮忙",
    "一下",
    "呢",
    "啊",
    "吧",
    "嘛",
    "哈",
    "哦",
    "呀",
    "啦",
)

# 仅用于检索阶段的同义词扩展，避免改写确定性 SQL 槽位抽取的原始条件值。
SYNONYMS = {
    "横缝": "横向裂缝",
    "纵缝": "纵向裂缝",
    "竖缝": "纵向裂缝",
    "龟裂": "网状裂缝",
    "网裂": "网状裂缝",
    "里程": "桩号",
    "定位": "GPS",
}


def normalize(message: str) -> str:
    text = message
    for word in FILLER_WORDS:
        text = text.replace(word, "")
    return re.sub(r"\s+", " ", text).strip()


def expand_for_retrieval(message: str) -> str:
    text = normalize(message)
    additions: list[str] = []
    for key, value in SYNONYMS.items():
        if key in text and value not in text:
            additions.append(value)
    if not additions:
        return text
    return f"{text} {' '.join(additions)}".strip()


def rewrite(message: str) -> str:
    return expand_for_retrieval(message)
