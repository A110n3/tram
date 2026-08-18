"""核心逻辑单元测试（不依赖真实后端）。

运行：python -m pytest tests/ -q   （或：python tests/test_core.py）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.backend import BackendError, StreamCancelled
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


def test_translate_context_uses_translation_not_original():
    """多块翻译时，上下文应传入前一块的译文（而非原文）。

    回归 bug：prev_chunk = chunk 传入了原文，导致 LLM 看到的
    "Previously translated content" 实际是未翻译的原文，
    无法保持术语/风格一致。
    """

    class _CaptureBackend:
        def __init__(self):
            self.messages_list = []
            self.call_count = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.call_count += 1
            self.messages_list.append(messages)
            if on_token:
                on_token(f"译文{self.call_count}")

    backend = _CaptureBackend()
    # 两段文本：各不超过 max_chars 下限（256），合计超出，确保分成两块
    # （段落超过 max_chars 时会被按句拆分，无法再作为整块参与测试）
    text = "第一段内容。" * 40 + "\n\n" + "第二段内容。" * 40
    config = {"backend": {}, "translation": {"chunk_chars": 200}}
    Translator(backend, config).translate(text)

    assert len(backend.messages_list) == 2
    # 第二块的 context_block 应包含译文（"译文1"），而非原文（"第一段内容"）
    second_messages = backend.messages_list[1]
    system_content = second_messages[0]["content"]
    assert "译文1" in system_content
    assert "第一段内容" not in system_content


def test_translate_join_preserves_paragraph_breaks():
    """多块翻译结果用 \\n\\n 连接，保留段落分隔。

    回归 bug：用 \\n 连接会丢失块间段落分隔。
    """

    class _StubBackend:
        def __init__(self):
            self.call_count = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.call_count += 1
            if on_token:
                on_token(f"块{self.call_count}")

    backend = _StubBackend()
    # 每段 160 字符（<256 下限）不触发按句拆分；合计 322 > 256 分成两块
    text = "第一段。" * 40 + "\n\n" + "第二段。" * 40
    config = {"backend": {}, "translation": {"chunk_chars": 200}}
    result = Translator(backend, config).translate(text)
    assert result == "块1\n\n块2"


def test_translate_propagates_stream_cancelled():
    """StreamCancelled 应穿透 translator 的重试循环，不触发重试。

    回归防护：取消异常非 BackendError 子类，_translate_chunk 不捕获它，
    直接传播到调用方。如果将来误加 except Exception 会吞掉取消信号。
    """

    class _CancelBackend:
        def __init__(self):
            self.call_count = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.call_count += 1
            raise StreamCancelled()

    backend = _CancelBackend()
    translator = Translator(backend, {"backend": {}})

    raised = False
    try:
        translator.translate("hello world")
    except StreamCancelled:
        raised = True

    assert raised, "StreamCancelled 应穿透 Translator.translate"
    # 只调用了一次：取消不应触发重试
    assert backend.call_count == 1


def test_translate_should_stop_prevents_any_request():
    """should_stop 已为 True 时不发起任何请求，直接抛 StreamCancelled。

    回归防护（取消竞态）：cancel 后旧任务若仍存活，不得在共享
    backend 上重新发起请求。
    """

    class _CountingBackend:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.calls += 1
            if on_token:
                on_token("ok")

    backend = _CountingBackend()
    translator = Translator(backend, {"backend": {}}, should_stop=lambda: True)
    raised = False
    try:
        translator.translate("hello")
    except StreamCancelled:
        raised = True
    assert raised
    assert backend.calls == 0


def test_translate_should_stop_between_chunks():
    """多块翻译中 should_stop 变 True：后续块不再发起请求。"""

    class _CountingBackend:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.calls += 1
            if on_token:
                on_token(f"块{self.calls}")

    stop = {"flag": False}
    backend = _CountingBackend()
    # 两块文本（各 160 字符 < 256 下限，合计超限）
    text = "第一段。" * 40 + "\n\n" + "第二段。" * 40
    config = {"backend": {}, "translation": {"chunk_chars": 200}}
    translator = Translator(backend, config, should_stop=lambda: stop["flag"])

    def on_token(_t):
        stop["flag"] = True  # 第一块流式输出时取消

    raised = False
    try:
        translator.translate(text, on_token=on_token)
    except StreamCancelled:
        raised = True
    assert raised
    assert backend.calls == 1


def test_translate_should_stop_blocks_retry():
    """请求失败后若 should_stop 变 True：不进入退避重试。"""

    class _FlakyBackend:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.calls += 1
            stop["flag"] = True  # 首次失败时刻恰好被取消
            raise BackendError("模拟瞬态失败", status_code=500)

    stop = {"flag": False}
    backend = _FlakyBackend()
    translator = Translator(backend, {"backend": {}}, should_stop=lambda: stop["flag"])
    retried = []
    raised = False
    try:
        translator.translate("hello", on_retry=lambda: retried.append(1))
    except StreamCancelled:
        raised = True
    assert raised
    assert backend.calls == 1
    assert not retried


def test_backoff_sleep_fallback_without_interruptible_sleep():
    """鸭子类型后端无 interruptible_sleep 时回退 time.sleep，返回 False。"""
    import app.core.translator as tr

    class _PlainBackend:
        pass

    sleeps: list[float] = []
    orig = tr.time.sleep
    tr.time.sleep = lambda s: sleeps.append(s)
    try:
        assert tr._backoff_sleep(_PlainBackend(), 1.5) is False
    finally:
        tr.time.sleep = orig
    assert sleeps == [1.5]


def test_translate_injects_glossary_into_prompt():
    """config 中的术语表必须注入提示词，强制模型使用指定译法。

    回归防护：术语表数据流为 glossary.json -> config["glossary"]
    -> to_prompt_block -> build_messages。任一环节断链都会导致
    术语表"保存了但不生效"。
    """

    class _CaptureBackend:
        def __init__(self):
            self.messages = None

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.messages = messages
            if on_token:
                on_token("ok")

    backend = _CaptureBackend()
    config = {
        "backend": {},
        "translation": {},
        "glossary": [{"source": "API", "target": "应用程序接口"}],
    }
    Translator(backend, config).translate("hello")

    system_content = backend.messages[0]["content"]
    assert "API => 应用程序接口" in system_content
    assert "Glossary" in system_content


def test_translate_no_glossary_no_block():
    """无术语表时提示词中不出现术语表块。"""

    class _CaptureBackend:
        def __init__(self):
            self.messages = None

        def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
            self.messages = messages
            if on_token:
                on_token("ok")

    backend = _CaptureBackend()
    Translator(backend, {"backend": {}, "glossary": []}).translate("hello")
    assert "Glossary" not in backend.messages[0]["content"]


def _patch_selection(selmod, fake_send_key, events):
    """替换 selection 的系统交互函数为探针，返回还原函数。"""
    orig = {
        name: getattr(selmod, name)
        for name in (
            "_wait_modifiers_released",
            "_set_clipboard_text",
            "_read_clipboard_text",
            "_send_key",
        )
    }
    selmod._wait_modifiers_released = lambda timeout_ms=300: None
    selmod._set_clipboard_text = lambda text, retries=8: True

    def fake_read(retries=5):
        events.append("read")
        return ""
    selmod._read_clipboard_text = fake_read
    selmod._send_key = fake_send_key

    def restore():
        for name, fn in orig.items():
            setattr(selmod, name, fn)
    return restore


def test_grab_selection_releases_keys_before_polling():
    """Ctrl+C 按下后必须先抬起、再轮询剪贴板。

    回归防护（v0.2.3 事故）：键抬起被移入 finally（轮询超时后才释放），
    但目标应用只在按键抬起后才把选区写入剪贴板，按住期间剪贴板恒为空，
    取词因此全部超时失败。
    """
    import app.core.selection as selmod

    events: list = []

    def fake_send_key(vk, up=False):
        events.append(("up" if up else "down", vk))
        return True

    restore = _patch_selection(selmod, fake_send_key, events)
    try:
        result = selmod.grab_selection(timeout_ms=40)
    finally:
        restore()
    assert result is None  # 未取到词，正常超时

    # 两个 key-up 必须发生在第一次轮询读取（第二个 read）之前
    first_poll_read = events.index("read", 1)
    assert events.index(("up", selmod.VK_C)) < first_poll_read
    assert events.index(("up", selmod.VK_CONTROL)) < first_poll_read


def test_grab_selection_releases_ctrl_when_c_down_fails():
    """C 按下失败时 Ctrl 已按下，finally 必须补发抬起防止 Ctrl 卡住。"""
    import app.core.selection as selmod

    events: list = []

    def fake_send_key(vk, up=False):
        events.append(("up" if up else "down", vk))
        # 模拟 C 键按下失败，其余按键均成功
        return not (vk == selmod.VK_C and not up)

    restore = _patch_selection(selmod, fake_send_key, events)
    try:
        result = selmod.grab_selection(timeout_ms=40)
    finally:
        restore()
    assert result is None
    assert ("up", selmod.VK_CONTROL) in events


def _grab_with_initial_clipboard(initial: str):
    """以指定初始剪贴板内容执行一次取词，返回 (结果, set 调用序列)。"""
    import app.core.selection as selmod

    set_calls: list[str] = []
    state = {"clipboard": initial}

    def fake_set(text, retries=8):
        set_calls.append(text)
        state["clipboard"] = text
        return True

    def fake_read(retries=5):
        return state["clipboard"]

    def fake_send_key(vk, up=False):
        # 模拟目标应用在按键抬起时把选区写入剪贴板
        if vk == selmod.VK_C and up:
            state["clipboard"] = "取到的文本"
        return True

    orig = {
        name: getattr(selmod, name)
        for name in (
            "_wait_modifiers_released",
            "_set_clipboard_text",
            "_read_clipboard_text",
            "_send_key",
        )
    }
    selmod._wait_modifiers_released = lambda timeout_ms=300: None
    selmod._set_clipboard_text = fake_set
    selmod._read_clipboard_text = fake_read
    selmod._send_key = fake_send_key
    try:
        return selmod.grab_selection(timeout_ms=200), set_calls
    finally:
        for name, fn in orig.items():
            setattr(selmod, name, fn)


def test_grab_selection_always_restores_clipboard():
    """取词结束后总是恢复剪贴板：原为空则置空（不留取词残留），
    原有内容则恢复原文。"""
    for initial in ("", "原有内容"):
        result, set_calls = _grab_with_initial_clipboard(initial)
        assert result == "取到的文本"
        # set 调用序列：[""(清空), initial(恢复)]，最后一步必须还原原内容
        assert set_calls[0] == ""
        assert set_calls[-1] == initial


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
