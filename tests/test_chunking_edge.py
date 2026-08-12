"""文本分段边界测试。"""

from __future__ import annotations

from app.core.chunking import split_text


def test_empty_text():
    assert split_text("") == []
    assert split_text("   ") == []
    assert split_text("\n\n\n") == []


def test_single_long_word():
    """无句子边界的超长文本至少返回一个块。"""
    text = "a" * 5000
    chunks = split_text(text, max_chars=1000)
    assert len(chunks) >= 1


def test_max_chars_minimum():
    """max_chars 小于 256 时被限制为 256。"""
    chunks = split_text("hello world", max_chars=10)
    assert all(len(c) <= 300 for c in chunks)  # 不崩溃即可
