"""聚焦控件检测 (Win32 API + ctypes)。

用于在 SendInput 直输前判断前台窗口是否有可接收键盘输入的聚焦控件，
避免把字符注入到没有任何焦点的窗口 (例如纯显示窗口、游戏画面)。

不变量 (AGENTS.md 5.5)：
- hwndFocus 非空即判定可输入（自绘 caret 的 Qt/Electron 编辑器不向
  Win32 报告 caret，但照常接受 KEYEVENTF_UNICODE 输入）。
- 仅在完全没有聚焦控件或 API 失败时保守返回 False，由上层走弹窗路径。
- 纯 Win32 ctypes，不依赖 Qt，可在 worker 线程调用。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

# ==================== Win32 常量 ====================

# GUITHREADINFO.flags 中与 caret 相关的位。
# 注意 GUI_CARETBLINKING 在 win32 头里是 0x00000001，GUI_CARETVISIBLE 等不常用于此判断。
_GUI_CARETBLINKING = 0x00000001

# ==================== 结构与函数原型 ====================

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]


class _RECT(ctypes.Structure):
    _fields_ = (
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    )


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", _RECT),
    )


_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []

_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

_user32.GetGUIThreadInfo.restype = wintypes.BOOL
_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]


def has_focused_caret() -> bool:
    """检查前台窗口线程是否有可接收键盘输入的聚焦控件。

    流程：GetForegroundWindow -> GetWindowThreadProcessId -> GetGUIThreadInfo。
    判据（宽松）：hwndFocus 非空即判定可输入——自绘 caret 的框架
    （Qt/Electron/现代编辑器）不向 Win32 报告 caret，但照常接受
    KEYEVENTF_UNICODE 输入；仅当完全没有聚焦控件（或 API 失败）时
    保守返回 False，由上层走弹窗路径。
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False

    thread_id = _user32.GetWindowThreadProcessId(hwnd, None)
    if not thread_id:
        return False

    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if not _user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        # API 失败：保守判定无 caret。
        return False

    if not info.hwndFocus:
        return False

    # caret 存在的判据：flags 标记闪烁，或 rcCaret 非全零矩形。
    if info.flags & _GUI_CARETBLINKING:
        return True

    rc = info.rcCaret
    if rc.left or rc.top or rc.right or rc.bottom:
        return True

    # 自绘 caret 的框架（Qt、Electron、多数现代编辑器）不向 Win32 报告
    # caret（hwndCaret=None、rcCaret 全零、无 GUI_CARETBLINKING），但它们
    # 照常接受 KEYEVENTF_UNICODE 输入。只要前台线程存在聚焦控件
    # （hwndFocus 非空，上面已检查）就乐观判定可输入；真正的非编辑控件
    # 会在 SendInput 层失败，由上层弹窗兜底。只有"完全没有聚焦控件"
    # 才保守拒绝。
    return True
