from __future__ import annotations

import re


def normalize(message: str) -> str:
    from .resource_loader import load_capability_file

    aliases = load_capability_file("aliases.yaml", {}) or {}
    config = aliases.get("query_rewrite") or {}
    text = message
    for word in config.get("fillers") or ():
        text = text.replace(word, "")
    return re.sub(r"\s+", " ", text).strip()


def expand_for_retrieval(message: str) -> str:
    from .resource_loader import load_capability_file

    aliases = load_capability_file("aliases.yaml", {}) or {}
    synonyms = (aliases.get("query_rewrite") or {}).get(
        "retrieval_synonyms"
    ) or {}
    text = normalize(message)
    additions: list[str] = []
    for key, value in synonyms.items():
        if key in text and value not in text:
            additions.append(value)
    if not additions:
        return text
    return f"{text} {' '.join(additions)}".strip()


def rewrite(message: str) -> str:
    return expand_for_retrieval(message)
