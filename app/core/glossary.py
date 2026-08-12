"""术语表管理。

术语表以 JSON 保存在 ~/.tram/glossary.json，
每条记录 {source: 原文, target: 译文}，翻译时注入提示词强制遵循。
写入采用原子替换，读取对损坏/畸形数据有防御。
"""

from __future__ import annotations

import json
import logging

from ..config import GLOSSARY_FILE, save_glossary_json

logger = logging.getLogger(__name__)

Entry = dict  # {"source": str, "target": str}


def ensure_file() -> None:
    GLOSSARY_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_glossary() -> list[Entry]:
    """读取术语表，对损坏/非 dict 元素有防御。

    - JSON 解析失败：返回空列表
    - 元素非 dict：跳过（不崩溃）
    - source/target 为空：跳过
    """
    try:
        if GLOSSARY_FILE.exists():
            with open(GLOSSARY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                result: list[Entry] = []
                for e in data:
                    if not isinstance(e, dict):
                        logger.warning("术语表跳过非 dict 条目: %r", e)
                        continue
                    src = e.get("source")
                    tgt = e.get("target")
                    if src and tgt:
                        result.append({"source": src, "target": tgt})
                return result
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("术语表读取失败，回退为空: %s", e)
    return []


def save_glossary(entries: list[Entry]) -> None:
    """原子写入术语表。"""
    save_glossary_json(entries)


def to_prompt_block(entries: list[Entry]) -> str:
    """把术语表转成提示词块，无条目时返回空字符串。"""
    if not entries:
        return ""
    lines = "\n".join(f"- {e['source']} => {e['target']}" for e in entries)
    return (
        "术语表（以下术语必须严格使用指定译文，不得意译）：\n"
        f"{lines}"
    )
