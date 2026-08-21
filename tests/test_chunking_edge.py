"""文本分段边界测试。"""

from __future__ import annotations

from app.core.chunking import split_text


def test_empty_text():
    assert split_text("") == []
    assert split_text("   ") == []
    assert split_text("\n\n\n") == []


def test_single_long_word():
    """无句子边界的超长文本：硬切兜底，块不超限且内容无损。"""
    text = "a" * 5000
    chunks = split_text(text, max_chars=1000)
    assert len(chunks) == 5
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks) == text


def test_unsplittable_mixed_into_paragraphs_still_bounded():
    """多段落中夹带无切分点超长文本：硬切兜底后所有块不超限。"""
    text = f"Short.\n\n{'x' * 900}\n\nShort."
    chunks = split_text(text, max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks).replace("\n\n", "").replace(" ", "") == (
        "Short." + "x" * 900 + "Short."
    )


def test_max_chars_minimum():
    """max_chars 小于 256 时被限制为 256。"""
    chunks = split_text("hello world", max_chars=10)
    assert all(len(c) <= 300 for c in chunks)  # 不崩溃即可


def test_long_paragraph_among_multiple_is_split():
    """多段落中夹带的超长段落也按句拆分，不得整段成为超限块。

    回归防护：OCR 常抓到一大坨无空行文本混在正常段落里，
    旧实现只有"唯一段落且超长"才切句，该段会原样超限。
    """
    short = "Short paragraph."
    long_para = ("This is one long sentence. " * 30).strip()  # ~809 字符
    assert len(long_para) > 256
    text = f"{short}\n\n{long_para}\n\n{short}"
    chunks = split_text(text, max_chars=256)
    assert len(chunks) > 2  # 长段被拆进多块
    assert all(len(c) <= 300 for c in chunks)
    # 短段保持完整，未被拆散
    assert sum(short in c for c in chunks) == 2


def test_cjk_sentence_split_inserts_no_space():
    """CJK 长段按句切分时，句子之间不插入空格。

    回归防护：旧实现用空格拼接句子，中文句间凭空多出空格，
    模型按"保留格式"要求翻译时会把空格也带进译文。
    """
    para = "这是一个中文句子。" * 100  # 900 字符，需按句切
    chunks = split_text(para, max_chars=300)
    assert len(chunks) > 1
    assert all("。 " not in c for c in chunks)
    # 内容无损：切出的块拼回（去掉块间分隔符）即原文
    assert "".join(chunks).replace("\n\n", "") == para


def test_english_sentence_split_keeps_space():
    """英文句子切分后仍以空格分隔，保留词边界。"""
    para = ("This is one English sentence. " * 50).strip()
    chunks = split_text(para, max_chars=300)
    assert len(chunks) > 1
    # 首块必含多个句子，句间应保留空格
    assert ". " in chunks[0]
