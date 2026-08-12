"""全局热键监听（Windows）。

用 Win32 RegisterHotKey 注册系统级热键，在监听线程的消息队列上接收
WM_HOTKEY。本模块只负责监听并 emit 信号；真正的取词动作在主线程槽中
执行（QClipboard 必须在主线程访问）。

parse_hotkey 为纯逻辑，不依赖 Win32，可单独单测。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PyQt6.QtCore import QThread, pyqtSignal

# Win32 消息常量
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# 修饰键标志位（RegisterHotKey 的 fuModifiers）
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_MOD_NAMES = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}

# 功能/特殊键 -> 虚拟键码
_VK_SPECIAL = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


class HotkeyError(Exception):
    """热键解析或注册异常。"""


def parse_hotkey(spec: str) -> tuple[int, int]:
    """解析热键字符串，返回 (modifiers, vk)。

    例:
        "Ctrl+Shift+T" -> (MOD_CONTROL | MOD_SHIFT, 0x54)
        "Alt+Q"        -> (MOD_ALT, 0x51)
        "Ctrl+F1"      -> (MOD_CONTROL, 0x70)

    最后一项为按键，其余为修饰键。大小写不敏感。
    """
    if not spec or not spec.strip():
        raise HotkeyError("热键不能为空")
    parts = [p.strip() for p in spec.replace("+", " ").split() if p.strip()]
    if not parts:
        raise HotkeyError("热键不能为空")
    if len(parts) < 2:
        raise HotkeyError("热键需至少含一个修饰键与一个按键，如 Ctrl+Shift+T")

    *mods, key = parts
    modifiers = 0
    for m in mods:
        ml = m.lower()
        if ml not in _MOD_NAMES:
            raise HotkeyError(f"未知的修饰键：{m}")
        modifiers |= _MOD_NAMES[ml]

    kl = key.lower()
    if kl in _VK_SPECIAL:
        vk = _VK_SPECIAL[kl]
    elif len(kl) == 1 and kl.isalpha():
        vk = ord(kl.upper())  # A-Z 的 vk 即大写 ASCII
    elif len(kl) == 1 and kl.isdigit():
        vk = ord(kl)  # 0-9 的 vk 即 ASCII
    else:
        raise HotkeyError(f"不支持的按键：{key}")
    return modifiers, vk


class GlobalHotkeyThread(QThread):
    """在独立线程注册并监听全局热键，触发时 emit triggered。"""

    triggered = pyqtSignal()
    registration_ok = pyqtSignal()
    registration_failed = pyqtSignal(str)

    def __init__(self, spec: str, hotkey_id: int = 1, parent=None):
        super().__init__(parent)
        self._spec = spec
        self._id = hotkey_id
        self._thread_id: int = 0

    def run(self) -> None:
        try:
            modifiers, vk = parse_hotkey(self._spec)
        except HotkeyError as e:
            self.registration_failed.emit(str(e))
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT
        ]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        self._thread_id = kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(None, self._id, modifiers, vk):
            self.registration_failed.emit(
                f"热键 {self._spec} 注册失败，可能已被其他程序占用，请在设置中更换。"
            )
            return
        self.registration_ok.emit()

        msg = wintypes.MSG()
        try:
            # GetMessageW: >0 正常消息，0=WM_QUIT，-1=错误
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self._id:
                    self.triggered.emit()
        finally:
            user32.UnregisterHotKey(None, self._id)

    def request_quit(self) -> None:
        """从主线程唤醒阻塞的 GetMessage，使监听线程退出。"""
        tid = self._thread_id
        if not tid:
            return
        user32 = ctypes.windll.user32
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
