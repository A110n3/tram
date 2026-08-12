"""全局热键监听（Windows）。

用 Win32 RegisterHotKey 注册系统级热键，消息循环使用
MsgWaitForMultipleObjects 同时等待 Windows 消息和停止事件，
避免 PostThreadMessageW(WM_QUIT) 在线程消息队列尚未创建时的
竞态丢失。

Win32 调用统一使用 pywin32（win32gui/win32event）。注意
RegisterHotKey 失败时 pywin32 抛出 pywintypes.error（而非返回
FALSE），错误码在 e.winerror。

本模块只负责监听并 emit 信号；真正的取词动作在主线程槽中
执行（QClipboard 必须在主线程访问）。

parse_hotkey 为纯逻辑，不依赖 Win32，可单独单测。
"""

from __future__ import annotations

import logging

import pywintypes
import win32event
import win32gui
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

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
        "Ctrl+F4"     -> (MOD_CONTROL, 0x73)
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
        raise HotkeyError("热键需至少含一个修饰键与一个按键，如 Ctrl+F4")

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
    """在独立线程注册并监听全局热键，触发时 emit triggered。

    使用 Win32 Event 作为停止信号，通过 MsgWaitForMultipleObjects
    在消息循环中同时等待消息和停止事件，从根本上避免
    PostThreadMessageW(WM_QUIT) 在线程消息队列创建前的竞态丢失。
    """

    triggered = pyqtSignal()
    registration_ok = pyqtSignal()
    registration_failed = pyqtSignal(str)

    def __init__(self, spec: str, hotkey_id: int = 1, parent=None):
        super().__init__(parent)
        self._spec = spec
        self._id = hotkey_id
        # 在主线程创建停止事件（manual-reset, initially non-signaled）
        try:
            self._stop_event = win32event.CreateEvent(None, True, False, None)
        except pywintypes.error:
            logger.error("CreateEvent 失败，停止机制将不可用", exc_info=True)
            self._stop_event = None

    def run(self) -> None:
        try:
            modifiers, vk = parse_hotkey(self._spec)
        except HotkeyError as e:
            self.registration_failed.emit(str(e))
            return

        try:
            win32gui.RegisterHotKey(None, self._id, modifiers, vk)
        except pywintypes.error as e:
            self.registration_failed.emit(
                f"热键 {self._spec} 注册失败: {e.strerror} (0x{e.winerror:08X})"
            )
            return
        self.registration_ok.emit()

        handles = [self._stop_event] if self._stop_event else []
        try:
            while True:
                # 同时等待：停止事件（索引 0）或任意 Windows 消息
                result = win32event.MsgWaitForMultipleObjects(
                    handles, False, win32event.INFINITE, win32event.QS_ALLINPUT
                )
                if handles and result == win32event.WAIT_OBJECT_0:
                    break  # 停止事件
                # 排空消息队列
                while True:
                    found, msg = win32gui.PeekMessage(None, 0, 0, 1)  # PM_REMOVE
                    if not found:
                        break
                    _hwnd, message, wparam, _lparam, _time, _pt = msg
                    if message == WM_HOTKEY and wparam == self._id:
                        self.triggered.emit()
                    elif message == WM_QUIT:
                        return  # 外部 WM_QUIT，直接退出
        finally:
            try:
                win32gui.UnregisterHotKey(None, self._id)
            except Exception:
                logger.debug("UnregisterHotKey 异常", exc_info=True)

    def request_quit(self) -> None:
        """从主线程触发停止事件，唤醒 MsgWaitForMultipleObjects。"""
        if self._stop_event:
            win32event.SetEvent(self._stop_event)


def test_hotkey_available(spec: str, timeout_ms: float = 300) -> tuple[bool, str]:
    """在临时线程上尝试注册热键，检测是否可注册（未被占用）。

    返回 (True, "") 或 (False, 错误消息)。
    注册成功后立即释放，不干扰实际热键监听。
    """
    import threading as _threading

    try:
        modifiers, vk = parse_hotkey(spec)
    except HotkeyError as e:
        return False, str(e)

    result: dict = {"ok": False, "error": ""}

    def _tester() -> None:
        TEST_ID = 0xBFFF  # 独立 ID，避免与主监听线程冲突
        try:
            win32gui.RegisterHotKey(None, TEST_ID, modifiers, vk)
            result["ok"] = True
            win32gui.UnregisterHotKey(None, TEST_ID)
        except pywintypes.error as e:
            result["error"] = (
                f"热键 {spec} 注册失败 (0x{e.winerror:08X})，请更换其他组合键"
            )
        except Exception:
            logger.debug("热键测试异常", exc_info=True)
            result["error"] = f"热键 {spec} 注册失败，请更换其他组合键"

    t = _threading.Thread(target=_tester, daemon=True)
    t.start()
    t.join(timeout_ms / 1000.0)

    return result["ok"], result["error"]
