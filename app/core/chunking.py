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
            buf = f"{buf} {sentence}".strip() if buf else sentence
    if buf:
        parts.append(buf)
    return parts


def split_text(text: str, max_chars: int = 2000) -> list[str]:
    """把文本切成若干块，以段落为基本单元。

    - 多段输入：段落是原子单元，只贪婪合并、不按句拆分，保证每个段落
      恰好对应一个块。这样译文按 ``\\n\\n`` 拼接时段落边界不会被拆开，
      避免在段落中间引入多余的空行。
    - 单段超长输入：没有段落边界可依托，退而按句子切，避免整块超出
      上下文上限。
    """
    if not text.strip():
        return []
    max_chars = max(256, int(max_chars))

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]

    # 唯一的段落且超长：只能按句子切
    if len(paragraphs) == 1 and len(paragraphs[0]) > max_chars:
        return _split_long_paragraph(paragraphs[0], max_chars)

    # 段落为原子单元，贪婪合并到接近 max_chars
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if buf and len(buf) + len(para) + 2 > max_chars:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks
