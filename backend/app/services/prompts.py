"""LLM 提示词集中管理：所有提示词都在项目根目录 `prompts/` 下。

为什么抽出来：以前提示词散在各 service 的 f-string 里，改一条规则要翻代码，
也没法整体 review——出现过"两条规则互相打架"的 bug（例如一边说"必须 GROUP BY"，
另一边说"多少处不要 GROUP BY"）。

文件格式（`prompts/<名字>.md`）：

    <!-- 可选说明注释，加载时会被剥掉 -->
    [system]
    系统提示词正文……

    [user]
    用户提示词正文……（用 <<占位符>> 填空）

占位符用 `<<name>>` 而不是 `{name}`：提示词里有大量 JSON / SQL 花括号，
用 str.format 会被当成占位符直接报错。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from .logging_service import log_error


PROMPT_DIR = PROJECT_ROOT / "prompts"

_COMMENT = re.compile(r"^\s*<!--.*?-->", re.S)
_SECTION = re.compile(r"^\[(system|user)\]\s*$")
_PLACEHOLDER = re.compile(r"<<[a-z_][a-z0-9_]*>>")


def prompt_path(name: str) -> Path:
    return PROMPT_DIR / f"{name}.md"


@lru_cache(maxsize=32)
def load(name: str) -> str:
    """读提示词原文（已剥掉文件头的说明注释）。"""
    path = prompt_path(name)
    if not path.exists():
        log_error("prompts", "missing", FileNotFoundError(str(path)), prompt=name)
        return ""
    text = path.read_text(encoding="utf-8")
    return _COMMENT.sub("", text, count=1).strip()


@lru_cache(maxsize=32)
def sections(name: str) -> tuple[tuple[str, str], ...]:
    """按 [system] / [user] 切成段落（提示词文件里的角色划分）。"""
    items: list[tuple[str, str]] = []
    role: str | None = None
    buffer: list[str] = []
    for line in load(name).splitlines():
        matched = _SECTION.match(line.strip())
        if matched:
            if role:
                items.append((role, "\n".join(buffer).strip()))
            role, buffer = matched.group(1), []
            continue
        if role:
            buffer.append(line)
    if role:
        items.append((role, "\n".join(buffer).strip()))
    return tuple(items)


def _fill(text: str, name: str, values: dict[str, Any]) -> str:
    for key, value in values.items():
        text = text.replace(f"<<{key}>>", str(value))
    leftover = _PLACEHOLDER.search(text)
    if leftover:
        # 提示词里写了占位符但调用方没传值——别静默把 <<xxx>> 发给模型
        log_error("prompts", "unfilled", ValueError(leftover.group(0)), prompt=name)
    return text.strip()


def render(name: str, **values: Any) -> str:
    """填好占位符的完整提示词（单段场景用）。"""
    return _fill(load(name), name, values)


def messages(name: str, **values: Any) -> list[dict[str, str]]:
    """直接给 chat()/chat_json() 用的 messages 列表。"""
    return [
        {"role": role, "content": _fill(body, name, values)}
        for role, body in sections(name)
    ]


def clear_cache() -> None:
    load.cache_clear()
    sections.cache_clear()
