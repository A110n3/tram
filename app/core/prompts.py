"""翻译提示词模板。

翻译质量的上限由提示词决定。核心思路：
1. 系统角色：专业译者，只输出译文
2. 源语言：支持自动识别（默认）或显式指定，注入提示词辅助翻译
3. 术语表注入：强制使用指定术语
4. 前文上下文：保持前后术语与风格一致
5. 明确格式要求：保留换行/空行，不解释、不增删

注意：模板本身全部使用英文。部分本地后端（如 Ryzen AI ONNX 服务）
无法处理请求中的非 ASCII 字符（直接返回 5xx 且无响应体），
因此中文配置值（目标语言、源语言、风格）在此统一映射为英文等价物；
用户数据（原文、术语条目）保持原样不做转换。
"""

from __future__ import annotations

# 中文配置值 -> 提示词中的英文等价物；未知值原样透传
# 同时用于源语言和目标语言的映射
LANG_EN = {
    "中文（简体）": "Simplified Chinese",
    "中文（繁体）": "Traditional Chinese",
    "英语": "English",
    "日语": "Japanese",
    "韩语": "Korean",
    "法语": "French",
    "德语": "German",
    "俄语": "Russian",
    "西班牙语": "Spanish",
    "葡萄牙语": "Portuguese",
    "意大利语": "Italian",
    "阿拉伯语": "Arabic",
    "越南语": "Vietnamese",
    "泰语": "Thai",
    "印尼语": "Indonesian",
    "荷兰语": "Dutch",
    "波兰语": "Polish",
    "土耳其语": "Turkish",
    "印地语": "Hindi",
}

# 源语言选项列表：第一项为自动识别，其余为显式指定
SOURCE_LANGS: list[str] = ["自动识别"] + list(LANG_EN.keys())

# 目标语言选项列表
TARGET_LANGS: list[str] = list(LANG_EN.keys())

# 自动识别标识
AUTO_DETECT = "自动识别"

STYLE_EN = {
    "忠实原文": "faithful to the original text",
    "自然流畅": "natural and fluent",
    "简洁精炼": "concise and refined",
}

SYSTEM_TEMPLATE = """You are a professional, faithful translator.
{source_instruction}Translate the user-provided text into {target_lang}.

Requirements:
1. Stay faithful to the source: do not add, omit or explain anything.
   Output ONLY the translation, nothing else.
2. Preserve the original paragraphs, line breaks and blank lines exactly.
3. Translation style: {style}.
{glossary_block}
{context_block}"""


def build_messages(
    text: str,
    target_lang: str,
    source_lang: str = AUTO_DETECT,
    style: str = "忠实原文",
    glossary_block: str = "",
    context_block: str = "",
    merge_system: bool = False,
) -> list[dict]:
    """构造发给后端的 messages 列表。

    source_lang: 源语言，"自动识别" 时由模型自行判断，否则在提示词中
    明确指定源语言以辅助翻译。
    glossary_block / context_block 由调用方生成（见 glossary.to_prompt_block）。
    merge_system=True 时把系统提示词并入单条 user 消息，
    用于不支持 system 角色的后端。
    """
    # 源语言指令：自动识别时为空（模型天然支持），显式时注入提示
    if source_lang != AUTO_DETECT:
        src_en = LANG_EN.get(source_lang, source_lang)
        source_instruction = f"The source text is in {src_en}. "
    else:
        source_instruction = ""

    system = SYSTEM_TEMPLATE.format(
        source_instruction=source_instruction,
        target_lang=LANG_EN.get(target_lang, target_lang),
        style=STYLE_EN.get(style, style),
        glossary_block=glossary_block,
        context_block=context_block,
    )
    if merge_system:
        return [
            {"role": "user", "content": f"{system}\n\nText to translate:\n{text}"}
        ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Text to translate:\n{text}"},
    ]
