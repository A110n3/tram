"""文本分段。

长文本不能一次性塞进上下文，需要按段落/句子切块。
优先在段落边界切，单段超长时再按句子切，尽量不断句，
避免在句中断开导致翻译割裂。
"""

from __future__ import annotations

import re

# 句子切分点：全角句读后直接切；英文句点后跟空白才切（避免小数点/缩写误切）
_SENTENCE_SPLIT = re.compile(
    r"(?<=[。！？!?；;．…])|(?<=[.!?])(?=\s)"
)

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def _ends_with_cjk(text: str) -> bool:
    """判断文本是否以 CJK 字符/全角标点结尾（决定拼接时是否插空格）。"""
    if not text:
        return False
    cp = ord(text[-1])
    return (
        0x3000 <= cp <= 0x9FFF       # CJK 符号/标点 + 汉字
        or 0x3400 <= cp <= 0x4DBF    # 扩展 A
        or 0xF900 <= cp <= 0xFAFF    # 兼容汉字
        or 0xFF00 <= cp <= 0xFFEF    # 全角形式
    )


def _join_sentences(a: str, b: str) -> str:
    """拼接两句：前句以 CJK 字符结尾时不插空格，避免污染中文原文。"""
    return a + b if _ends_with_cjk(a) else f"{a} {b}"


def _split_long_paragraph(text: str, max_chars: int) -> list[str]:
    """把单个超长段落按句子切开。"""
    parts = []
    buf = ""
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if buf and len(buf) + len(sentence) + 1 > max_chars:
            parts.append(buf)
            buf = sentence
        else:
            buf = _join_sentences(buf, sentence) if buf else sentence
    if buf:
        parts.append(buf)
    return parts


def split_text(text: str, max_chars: int = 2000) -> list[str]:
    """把文本切成若干块，以段落为基本单元。

    - 多段输入：段落是原子单元，只贪婪合并、不按句拆分，保证每个段落
      恰好对应一个块。这样译文按 ``\\n\\n`` 拼接时段落边界不会被拆开，
      避免在段落中间引入多余的空行。
    - 单段超长输入（含多段落中夹带的超长段落）：没有段落边界可依托，
      退而按句子切，避免整块超出上下文上限。
    """
    if not text.strip():
        return []
    max_chars = max(256, int(max_chars))

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]

    # 超长段落（无论是否唯一）先按句子拆成不超限的单元
    units: list[str] = []
    for para in paragraphs:
        if len(para) > max_chars:
            units.extend(_split_long_paragraph(para, max_chars))
        else:
            units.append(para)

    # 单元贪婪合并到接近 max_chars
    chunks: list[str] = []
    buf = ""
    for unit in units:
        if buf and len(buf) + len(unit) + 2 > max_chars:
            chunks.append(buf)
            buf = unit
        else:
            buf = f"{buf}\n\n{unit}" if buf else unit
    if buf:
        chunks.append(buf)
    return chunks
