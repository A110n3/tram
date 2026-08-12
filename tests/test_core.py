"""核心逻辑单元测试（不依赖真实后端）。

运行：python -m pytest tests/ -q   （或：python tests/test_core.py）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.chunking import split_text
from app.core.glossary import to_prompt_block
from app.core.prompts import build_messages


def test_split_text_respects_max_chars():
    para = "这是第一段。" * 600  # 超长单段（约 3600 字符）
    chunks = split_text(para, max_chars=2000)
    assert len(chunks) > 1
    assert all(len(c) <= 2100 for c in chunks)  # 句子单元略超上限可容忍
    assert "".join(chunks).replace(" ", "") == para


def test_split_text_keeps_paragraph_boundaries():
    text = "段A。\n\n段B。\n\n段C。"
    chunks = split_text(text, max_chars=2000)
    assert len(chunks) == 1  # 很小，应合并为一块
    assert chunks[0] == text


def test_split_text_small_chunks():
    text = "\n\n".join([f"第{i}段。" for i in range(60)])
    chunks = split_text(text, max_chars=256)
    assert len(chunks) > 1
    assert all(c in text for c in chunks)


def test_glossary_to_prompt_block():
    entries = [{"source": "API", "target": "应用程序接口"}]
    block = to_prompt_block(entries)
    assert "API => 应用程序接口" in block
    assert to_prompt_block([]) == ""


def test_build_messages_contains_glossary_and_target():
    msgs = build_messages(
        "hello",
        target_lang="中文（简体）",
        glossary_block="术语表：\n- API => 接口",
        context_block="前文：你好",
    )
    assert msgs[0]["role"] == "system"
    assert "中文（简体）" in msgs[0]["content"]
    assert "API => 接口" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hello"}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failed else 0)
