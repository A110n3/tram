"""翻译提示词模板。

翻译质量的上限由提示词决定。核心思路：
1. 系统角色：专业译者，只输出译文
2. 术语表注入：强制使用指定术语
3. 前文上下文：保持前后术语与风格一致
4. 明确格式要求：保留换行/空行，不解释、不增删
"""

from __future__ import annotations

SYSTEM_TEMPLATE = """你是一位专业、忠实的翻译工作者。请把用户提供的文本翻译成{target_lang}。

翻译要求：
1. 忠实原文，不增删内容，不解释，不输出译文以外的任何文字。
2. 严格保留原文的段落、换行和空行格式。
3. 翻译风格：{style}。
{glossary_block}
{context_block}"""


def build_messages(
    text: str,
    target_lang: str,
    style: str = "忠实原文",
    glossary_block: str = "",
    context_block: str = "",
) -> list[dict]:
    """构造发给后端的 messages 列表。

    glossary_block / context_block 由调用方生成（见 glossary.to_prompt_block）。
    """
    system = SYSTEM_TEMPLATE.format(
        target_lang=target_lang,
        style=style,
        glossary_block=glossary_block,
        context_block=context_block,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
