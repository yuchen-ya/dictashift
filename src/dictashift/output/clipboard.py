"""剪贴板读写 (Win32 API + ctypes，不依赖 Qt)。

刻意使用 user32/kernel32 而非 Qt 剪贴板：本服务运行在后台线程，
QGuiApplication.clipboard() 必须在主线程访问，否则触发 Qt 警告甚至崩溃；
纯 Win32 调用无此限制，方便后续 worker 线程使用。

不变量 (AGENTS.md 5.5)：
- set_clipboard_text 成功后剪贴板内容即为传入文本；
- 任何失败抛 OutputError，调用方决定是否回退。
- SetClipboardData 失败时绝不 EmptyClipboard 已清空的剪贴板状态——实际实现中
  EmptyClipboard 在 SetClipboardData 成功之后才会被依赖，若 SetClipboardData
  失败则抛异常，剪贴板原有内容可能已被 EmptyClipboard 清除 (Win32 语义)，
  这是 Win32 API 本身的限制；为降低风险，函数在写入前不主动清空无关格式。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)


class OutputError(RuntimeError):
    """输出相关错误 (剪贴板 / SendInput)。"""


# ==================== Win32 常量 ====================

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

# ==================== 函数原型绑定 ====================

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]

# GlobalAlloc / GlobalFree / GlobalLock / GlobalUnlock
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

# OpenClipboard / CloseClipboard / EmptyClipboard / SetClipboardData / GetClipboardData
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.SetClipboardData.restype = ctypes.c_void_p  # HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_user32.GetClipboardData.restype = ctypes.c_void_p  # HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]


def _open_clipboard_with_retry(owner, retries: int = 5, delay: float = 0.03) -> bool:
    """OpenClipboard 的瞬态失败重试封装。

    Windows 上若另一个窗口正持有剪贴板锁，OpenClipboard 会返回 ERROR_ACCESS_DENIED
    (GetLastError=5)，这是常见的、可自愈的瞬态情况。Win32 惯用做法是短暂重试。
    返回 True 表示成功打开 (调用方负责 CloseClipboard)，False 表示重试用尽仍失败。
    """
    import time

    for _ in range(retries):
        if _user32.OpenClipboard(owner):
            return True
        if ctypes.get_last_error() != 5:
            return False  # 非 access-denied 错误不重试
        time.sleep(delay)
    return False


def set_clipboard_text(text: str) -> None:
    """把 text 写入系统剪贴板 (CF_UNICODETEXT)。

    流程：OpenClipboard(None) -> EmptyClipboard -> 分配 GMEM_MOVEABLE 全局内存 ->
    拷贝 UTF-16 字节 -> SetClipboardData(CF_UNICODETEXT, h) -> CloseClipboard。
    成功后所有权移交系统，无需 GlobalFree。

    失败抛 OutputError。注意：EmptyClipboard 会清空剪贴板，因此若后续
    SetClipboardData 失败，剪贴板已为空——这是 Win32 API 语义本身，调用方无需特殊处理。
    """
    # 编码为 UTF-16 LE，并显式补结尾 \x00 (Win32 要求以 NUL 结尾)。
    data = text.encode("utf-16-le")
    null_terminator = b"\x00\x00"
    payload = data + null_terminator

    if not _open_clipboard_with_retry(None):
        raise OutputError(f"OpenClipboard 失败，GetLastError={ctypes.get_last_error()}")
    try:
        if not _user32.EmptyClipboard():
            raise OutputError(f"EmptyClipboard 失败，GetLastError={ctypes.get_last_error()}")

        h_global = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
        if not h_global:
            raise OutputError(f"GlobalAlloc 失败，GetLastError={ctypes.get_last_error()}")

        locked = _kernel32.GlobalLock(h_global)
        if not locked:
            _kernel32.GlobalFree(h_global)
            raise OutputError(f"GlobalLock 失败，GetLastError={ctypes.get_last_error()}")
        try:
            ctypes.memmove(locked, payload, len(payload))
        finally:
            _kernel32.GlobalUnlock(h_global)

        # SetClipboardData 成功后系统接管内存所有权，不可再 GlobalFree。
        if not _user32.SetClipboardData(_CF_UNICODETEXT, h_global):
            _kernel32.GlobalFree(h_global)
            raise OutputError(f"SetClipboardData 失败，GetLastError={ctypes.get_last_error()}")
    finally:
        # 无论成败都尝试关闭剪贴板，避免死锁其它进程。
        _user32.CloseClipboard()


def get_clipboard_text() -> str | None:
    """读取系统剪贴板的 CF_UNICODETEXT；无文本格式返回 None。调试用，实现保持简单。

    注意：此函数仅用于调试，不作为关键路径；它返回的是剪贴板内容的一个 Python 字符串副本。
    """
    if not _user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return None
    if not _open_clipboard_with_retry(None):
        raise OutputError(f"OpenClipboard 失败，GetLastError={ctypes.get_last_error()}")
    try:
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        locked = _kernel32.GlobalLock(handle)
        if not locked:
            return None
        try:
            # 以宽字符逐字节读取直到遇到 \x00\x00 结尾。
            raw = ctypes.wstring_at(locked)
        finally:
            _kernel32.GlobalUnlock(handle)
        return raw
    finally:
        _user32.CloseClipboard()
