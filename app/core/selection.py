"""获取当前激活窗口的选中文本（Windows）。

通过模拟 Ctrl+C 把选区复制到剪贴板再读取。取词前后备份/恢复剪贴板
文本，尽量不污染原剪贴板。仅处理文本格式；图片/文件等非文本格式无法
备份恢复（已知限制，TODO: EnumClipboardFormats 全格式备份）。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

from PyQt6.QtWidgets import QApplication

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_C = 0x43


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT


def _send_key(vk: int, up: bool = False) -> None:
    """发送一次按键（按下或抬起）。"""
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def grab_selection(timeout_ms: int = 400) -> Optional[str]:
    """获取当前选中的文本。

    流程：备份剪贴板文本 -> 清空 -> 模拟 Ctrl+C -> 轮询等待剪贴板
    出现新文本 -> 返回；最后恢复原剪贴板文本。无选中或超时返回 None。
    """
    clip = QApplication.clipboard()
    old = clip.text()
    clip.setText("")  # 清空，便于检测复制是否产生了新内容

    try:
        _send_key(VK_CONTROL, up=False)
        _send_key(VK_C, up=False)
        _send_key(VK_C, up=True)
        _send_key(VK_CONTROL, up=True)

        deadline = time.monotonic() + timeout_ms / 1000.0
        text = ""
        while time.monotonic() < deadline:
            text = clip.text()
            if text:
                break
            time.sleep(0.02)
        return text or None
    finally:
        if old:
            clip.setText(old)
