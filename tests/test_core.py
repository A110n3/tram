"""核心逻辑单元测试（不依赖真实后端）。

运行：python -m pytest tests/ -q   （或：python tests/test_core.py）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.backend import BackendError
from app.core.chunking import split_text
from app.core.glossary import to_prompt_block
from app.core.prompts import build_messages
from app.core.translator import Translator


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
        glossary_block="Glossary:\n- API => 接口",
        context_block="前文：你好",
    )
    assert msgs[0]["role"] == "system"
    # 中文配置值在提示词中映射为英文（兼容不支持非 ASCII 的后端）
    assert "Simplified Chinese" in msgs[0]["content"]
    assert "中文（简体）" not in msgs[0]["content"]
    # 术语条目等用户数据保持原样
    assert "API => 接口" in msgs[0]["content"]
    # user 消息带 "Text to translate:" 前缀，避免小模型把裸文本当成聊天问候
    assert msgs[1] == {"role": "user", "content": "Text to translate:\nhello"}


def test_build_messages_auto_detect_source():
    """源语言为自动识别时，提示词中不包含源语言指定。"""
    msgs = build_messages("hello", target_lang="英语", source_lang="自动识别")
    system = msgs[0]["content"]
    assert "Translate the user-provided text into English" in system
    assert "source text is in" not in system.lower()


def test_build_messages_explicit_source_lang():
    """源语言为显式指定时，提示词中注入源语言信息。"""
    msgs = build_messages("hello", target_lang="中文（简体）", source_lang="英语")
    system = msgs[0]["content"]
    assert "The source text is in English." in system
    assert "Simplified Chinese" in system


def test_build_messages_unknown_source_lang_passthrough():
    """未知源语言值原样透传为英文提示。"""
    msgs = build_messages("hello", target_lang="英语", source_lang=" Klingon")
    system = msgs[0]["content"]
    assert " Klingon" in system


def test_build_messages_user_message_has_label():
    """user 消息必须带 "Text to translate:" 前缀，避免小模型把
    单词/短语等裸文本误当成聊天问候（回归 bug）。"""
    # 普通路径
    msgs = build_messages("hello", target_lang="中文（简体）")
    assert msgs[1]["content"] == "Text to translate:\nhello"
    # merge_system 路径同样带前缀
    msgs = build_messages("hello", target_lang="中文（简体）", merge_system=True)
    assert "Text to translate:\nhello" in msgs[0]["content"]


def test_build_messages_merge_system():
    """merge_system 模式：指令并入单条 user 消息（兼容不支持 system 的后端）。"""
    msgs = build_messages(
        "hello", target_lang="中文（简体）", merge_system=True
    )
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "Simplified Chinese" in msgs[0]["content"]
    assert "hello" in msgs[0]["content"]


def test_is_retryable_status_ranges():
    """4xx 为永久错误不重试；5xx 与无状态码错误可重试（回归 bug）。"""
    from app.core.translator import _is_retryable

    for code in (400, 401, 404, 422, 499):
        assert not _is_retryable(BackendError("x", status_code=code)), code
    for code in (500, 502, 503):
        assert _is_retryable(BackendError("x", status_code=code)), code
    assert _is_retryable(BackendError("网络错误"))


def test_translate_merges_system_when_disabled():
    """use_system_role=False 时，发给后端的是单条 user 消息。"""

    class _CaptureBackend:
        def __init__(self):
            self.messages = None

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.messages = messages
            if on_token:
                on_token("译文")

    backend = _CaptureBackend()
    config = {"backend": {"use_system_role": False}, "translation": {}}
    Translator(backend, config).translate("hello")
    assert backend.messages is not None
    assert len(backend.messages) == 1
    assert backend.messages[0]["role"] == "user"

    # 默认仍使用 system 角色
    backend2 = _CaptureBackend()
    Translator(backend2, {"backend": {}, "translation": {}}).translate("hello")
    assert backend2.messages[0]["role"] == "system"
    assert backend2.messages[1]["role"] == "user"


def test_translate_reads_source_lang_from_config():
    """Translator 从 config 读取 source_lang 并注入提示词。"""

    class _CaptureBackend:
        def __init__(self):
            self.messages = None

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.messages = messages
            if on_token:
                on_token("译文")

    # 显式源语言
    backend = _CaptureBackend()
    config = {"backend": {}, "translation": {"source_lang": "英语"}}
    Translator(backend, config).translate("hello")
    assert "The source text is in English." in backend.messages[0]["content"]

    # 自动识别（默认）
    backend2 = _CaptureBackend()
    Translator(backend2, {"backend": {}, "translation": {}}).translate("hello")
    assert "source text is in" not in backend2.messages[0]["content"].lower()


def test_translate_chunk_clears_partial_tokens_on_retry():
    """重试时必须清空前次部分产出并通知回滚，避免译文重复（回归 bug）。"""
    import app.core.translator as tr

    class _FlakyBackend:
        def __init__(self):
            self.attempt = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.attempt += 1
            if self.attempt == 1:
                if on_token:
                    on_token("残留")
                raise BackendError("模拟首次失败")
            if on_token:
                on_token("正确译文")

    backend = _FlakyBackend()
    translator = Translator(backend, {"backend": {}})
    emitted: list[str] = []
    retry_count = 0

    def on_retry() -> None:
        nonlocal retry_count
        retry_count += 1
        emitted.clear()  # 模拟 UI 回滚本块已显示内容

    orig_sleep = tr.time.sleep
    tr.time.sleep = lambda s: None  # 屏蔽退避，加快测试
    try:
        result = translator._translate_chunk(
            [{"role": "user", "content": "hi"}],
            on_token=lambda t: emitted.append(t),
            on_retry=on_retry,
        )
    finally:
        tr.time.sleep = orig_sleep

    assert result == "正确译文"
    assert retry_count == 1
    assert "".join(emitted) == "正确译文"


def test_parse_hotkey_ctrl_shift_t():
    from app.core.hotkey import MOD_CONTROL, MOD_SHIFT, parse_hotkey
    mods, vk = parse_hotkey("Ctrl+Shift+T")
    assert mods == (MOD_CONTROL | MOD_SHIFT)
    assert vk == 0x54  # 'T'


def test_parse_hotkey_alt_q():
    from app.core.hotkey import MOD_ALT, parse_hotkey
    mods, vk = parse_hotkey("Alt+Q")
    assert mods == MOD_ALT
    assert vk == 0x51  # 'Q'


def test_parse_hotkey_no_mod():
    from app.core.hotkey import HotkeyError, parse_hotkey
    try:
        parse_hotkey("T")
        assert False, "应抛 HotkeyError"
    except HotkeyError:
        pass


def test_parse_hotkey_invalid_mod():
    from app.core.hotkey import HotkeyError, parse_hotkey
    try:
        parse_hotkey("Super+T")
        assert False, "应抛 HotkeyError"
    except HotkeyError:
        pass


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
