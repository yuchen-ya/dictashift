"""聚焦控件检测 (Win32 API + ctypes)。

用于在 SendInput 直输前判断前台窗口是否有可接收键盘输入的聚焦控件，
避免把字符注入到没有任何焦点的窗口 (例如纯显示窗口、游戏画面)。

不变量 (AGENTS.md 5.5)：
- hwndFocus 非空即判定可输入（自绘 caret 的 Qt/Electron 编辑器不向
  Win32 报告 caret，但照常接受 KEYEVENTF_UNICODE 输入）。
- 例外：shell 外壳窗口类（Progman/WorkerW 桌面、Shell_TrayWnd 任务栏等）
  虽然有"聚焦控件"（桌面图标列表 / 任务栏按钮），但并非文本输入框；
  命中黑名单时直接返回 False，由上层走弹窗路径，避免 SendInput 把
  字符打进桌面/任务栏被吞而误以为 pasted 成功。
- 仅在完全没有聚焦控件、命中 shell 黑名单或 API 失败时保守返回 False。
- 纯 Win32 ctypes，不依赖 Qt，可在 worker 线程调用。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

# ==================== Win32 常量 ====================

# GUITHREADINFO.flags 中与 caret 相关的位。
# 注意 GUI_CARETBLINKING 在 win32 头里是 0x00000001，GUI_CARETVISIBLE 等不常用于此判断。
_GUI_CARETBLINKING = 0x00000001

# Shell 外壳窗口类黑名单：这些窗口的"聚焦控件"是桌面图标列表 / 任务栏按钮 /
# 通知区域图标等，并非文本输入框。前台落在它们上面（用户点击桌面或任务栏）
# 时即使 hwndFocus 非空也不应判定可输入——SendInput 会被吞掉，
# 上层误以为已粘贴成功而不再走弹窗兜底。比较时大小写不敏感。
_NON_EDITABLE_SHELL_CLASSES = frozenset(
    {
        "Progman",                  # 桌面（壁纸/图标层）
        "WorkerW",                  # 桌面（动画壁纸层，与 Progman 交替前台）
        "Shell_TrayWnd",            # 主任务栏
        "Shell_SecondaryTrayWnd",   # 多显示器副任务栏
        "NotifyIconOverflowWindow", # 通知区域溢出窗口
        "TaskSwitcherWnd",          # Alt+Tab 任务切换器
    }
)

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

# GetClassNameW：取窗口类名。原型 int WINAPI GetClassNameW(HWND, LPWSTR, int)。
_user32.GetClassNameW.restype = wintypes.INT
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]


def _is_non_editable_shell_target(class_name: str) -> bool:
    """判断窗口类名是否属于不可输入文本的 shell 外壳窗口。

    覆盖：桌面（Progman/WorkerW）、任务栏（Shell_TrayWnd /
    Shell_SecondaryTrayWnd）、通知溢出区（NotifyIconOverflowWindow）、
    Alt+Tab 切换器（TaskSwitcherWnd）。这些目标虽有"聚焦控件"
    （图标列表 / 按钮），但并非文本输入框，命中即应拒绝直输。
    比较大小写不敏感（Win32 类名约定大小写不敏感）。
    """
    if not class_name:
        return False
    return class_name.lower() in _NON_EDITABLE_SHELL_CLASSES_LOWER


# 黑名单的小写形式（模块级常量，避免每次调用都重新 lowercase）。
_NON_EDITABLE_SHELL_CLASSES_LOWER = frozenset(
    c.lower() for c in _NON_EDITABLE_SHELL_CLASSES
)


def _get_class_name(hwnd: int) -> str | None:
    """安全取窗口类名；失败（含空 hwnd）返回 None。

    GetClassNameW 失败或返回 0 时返回 None，调用方据此忽略该检查
    （不因取类名失败而误判为非编辑目标）。
    """
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(256)
    got = _user32.GetClassNameW(hwnd, buf, len(buf))
    if not got:
        return None
    return buf.value


def has_focused_caret() -> bool:
    """检查前台窗口线程是否有可接收键盘输入的聚焦控件。

    流程：GetForegroundWindow -> GetWindowThreadProcessId -> GetGUIThreadInfo。
    判据（宽松）：hwndFocus 非空即判定可输入——自绘 caret 的框架
    （Qt/Electron/现代编辑器）不向 Win32 报告 caret，但照常接受
    KEYEVENTF_UNICODE 输入；仅当完全没有聚焦控件（或 API 失败）时
    保守返回 False，由上层走弹窗路径。

    例外（shell 黑名单）：即便 hwndFocus 非空，若前台窗口或聚焦控件
    的类名命中桌面/任务栏等 shell 外壳类（Progman/WorkerW/Shell_TrayWnd/
    Shell_SecondaryTrayWnd/NotifyIconOverflowWindow/TaskSwitcherWnd），
    视为不可输入文本，返回 False。GetClassNameW 失败时忽略该检查
    （维持原有乐观判据，不因此误判为不可输入）。
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

    # Shell 外壳黑名单：用户点击桌面/任务栏后，shell 窗口会有"聚焦控件"
    # （图标列表 / 按钮），但并非文本输入框。取前台窗口与聚焦控件的类名，
    # 任一命中即拒绝直输，交给上层弹窗兜底。GetClassNameW 失败则忽略，
    # 不影响后续乐观判据。
    for probe_hwnd in (hwnd, info.hwndFocus):
        class_name = _get_class_name(probe_hwnd)
        if class_name is not None and _is_non_editable_shell_target(class_name):
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
