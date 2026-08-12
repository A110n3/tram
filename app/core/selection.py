"""获取当前激活窗口的选中文本（Windows）。

通过模拟 Ctrl+C 把选区复制到剪贴板再读取。取词前后备份/恢复剪贴板
文本，尽量不污染原剪贴板。仅处理文本格式；图片/文件等非文本格式无法
备份恢复（已知限制，TODO: EnumClipboardFormats 全格式备份）。

剪贴板清空失败时不会返回旧内容（避免误报）。
"""

from __future__ import annotations

import logging
import time

import win32api
from PyQt6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12    # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_C = 0x43

# grab_selection 前等待这些键全部释放，避免热键修饰键干扰 SendInput(Ctrl+C)
_MODIFIER_VKS = (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)


def _send_key(vk: int, up: bool = False) -> bool:
    """发送一次按键（按下或抬起）。返回是否成功。"""
    flags = KEYEVENTF_KEYUP if up else 0
    try:
        win32api.keybd_event(vk, 0, flags, 0)
        return True
    except Exception:
        logger.debug("keybd_event 失败 vk=0x%02X", vk, exc_info=True)
        return False


def _set_clipboard_text(text: str, retries: int = 8) -> bool:
    """设置剪贴板文本，带重试与退避。返回是否成功。

    连续调用之间使用递增延迟（50ms -> 400ms），提高与剪贴板管理
    工具（Ditto 等）竞争临界区的成功率。所有失败静默收敛为
    False 返回，调用方自行决定降级策略。
    """
    clip = QApplication.clipboard()
    if clip is None:
        return False
    for i in range(retries):
        try:
            clip.setText(text)
            return True
        except Exception:
            if i < retries - 1:
                delay = min(0.05 * (1.5 ** i), 0.4)
                time.sleep(delay)
            else:
                logger.debug("set_clipboard 失败，已重试 %d 次", retries)
    return False


def _read_clipboard_text(retries: int = 5) -> str:
    """读取剪贴板文本，带重试。失败返回空字符串。"""
    clip = QApplication.clipboard()
    if clip is None:
        return ""
    for i in range(retries):
        try:
            return clip.text()
        except Exception:
            if i < retries - 1:
                time.sleep(0.05)
            else:
                logger.debug("read_clipboard 失败，已重试 %d 次", retries)
    return ""


def _wait_modifiers_released(timeout_ms: float = 300) -> None:
    """等待所有修饰键释放，避免热键修饰键干扰 SendInput(Ctrl+C)。

    用户按下热键（如 Ctrl+Shift+F4）后，Ctrl 和 Shift 可能仍被按住。
    此时 SendInput 发出的 Ctrl+C 会被 Windows 合并为 Ctrl+Shift+C，
    导致目标应用不识别"复制"操作。此函数轮询 GetAsyncKeyState，
    直到所有修饰键松开或超时。
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if not any(
            win32api.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS
        ):
            return
        time.sleep(0.01)


def grab_selection(timeout_ms: int = 400) -> str | None:
    """获取当前选中的文本。

    流程：等待修饰键释放 -> 备份剪贴板文本 -> 清空 -> 模拟 Ctrl+C
    -> 轮询等待剪贴板出现新文本 -> 返回；最后恢复原剪贴板文本。
    无选中或超时返回 None。

    剪贴板操作含重试和静默失败，避免 COM error 0x800401d0 崩溃。
    清空失败时直接返回 None，避免返回旧剪贴板内容（误报）。
    """
    # 关键：等用户松开热键的修饰键，否则 Ctrl+C 变 Ctrl+Shift+C 等
    _wait_modifiers_released()

    old = _read_clipboard_text()

    # 清空剪贴板以便检测 Ctrl+C 是否产出了新内容
    # 清空失败时无法区分新旧内容，直接放弃
    if not _set_clipboard_text(""):
        logger.warning("剪贴板清空失败，跳过取词以避免误报")
        return None

    try:
        _send_key(VK_CONTROL, up=False)
        _send_key(VK_C, up=False)
        _send_key(VK_C, up=True)
        _send_key(VK_CONTROL, up=True)

        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            text = _read_clipboard_text(retries=1)
            if text:
                return text
            time.sleep(0.02)
        return None  # 超时，无选中内容
    finally:
        # 尽力恢复原剪贴板；失败则静默
        if old:
            _set_clipboard_text(old)
