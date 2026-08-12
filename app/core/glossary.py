"""术语表管理。

术语表以 JSON 保存在 ~/.tram/glossary.json，
每条记录 {source: 原文, target: 译文}，翻译时注入提示词强制遵循。
"""

from __future__ import annotations

import json
import os

from ..config import GLOSSARY_FILE

Entry = dict  # {"source": str, "target": str}


def ensure_file() -> None:
    GLOSSARY_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_glossary() -> list[Entry]:
    try:
        if GLOSSARY_FILE.exists():
            with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [e for e in data if e.get("source") and e.get("target")]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_glossary(entries: list[Entry]) -> None:
    ensure_file()
    with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def to_prompt_block(entries: list[Entry]) -> str:
    """把术语表转成提示词块，无条目时返回空字符串。"""
    if not entries:
        return ""
    lines = "\n".join(f"- {e['source']} => {e['target']}" for e in entries)
    return (
        "术语表（以下术语必须严格使用指定译文，不得意译）：\n"
        f"{lines}"
    )
