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
        glossary_block="术语表：\n- API => 接口",
        context_block="前文：你好",
    )
    assert msgs[0]["role"] == "system"
    assert "中文（简体）" in msgs[0]["content"]
    assert "API => 接口" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hello"}


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
    from app.core.hotkey import parse_hotkey, HotkeyError, MOD_CONTROL, MOD_SHIFT
    mods, vk = parse_hotkey("Ctrl+Shift+T")
    assert mods == (MOD_CONTROL | MOD_SHIFT)
    assert vk == 0x54  # 'T'


def test_parse_hotkey_alt_q():
    from app.core.hotkey import parse_hotkey, MOD_ALT
    mods, vk = parse_hotkey("Alt+Q")
    assert mods == MOD_ALT
    assert vk == 0x51  # 'Q'


def test_parse_hotkey_no_mod():
    from app.core.hotkey import parse_hotkey, HotkeyError
    try:
        parse_hotkey("T")
        assert False, "应抛 HotkeyError"
    except HotkeyError:
        pass


def test_parse_hotkey_invalid_mod():
    from app.core.hotkey import parse_hotkey, HotkeyError
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
