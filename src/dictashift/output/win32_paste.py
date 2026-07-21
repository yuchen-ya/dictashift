"""通过 Win32 SendInput 模拟 Ctrl+V 粘贴。

不变量 (AGENTS.md 5.5)：
- 默认流程为：写入剪贴板 -> SendInput 模拟粘贴。
- 无法注入到高完整性级别窗口时 (例如目标以管理员身份运行、本进程不是)，
  paste_text 返回 False，且剪贴板内容必须保留，由上层提示用户手动 Ctrl+V。
- 未经授权，不默认要求主程序以管理员身份运行。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from .clipboard import OutputError, set_clipboard_text

logger = logging.getLogger(__name__)


# ==================== Win32 常量 ====================

_VK_CONTROL = 0x11
_VK_V = 0x56
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD = 1

# ==================== SendInput 结构与函数原型 ====================

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),  # ULONG_PTR
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 64))


class _INPUT(ctypes.Structure):
    _anonymous_ = ("_anon",)
    _fields_ = (
        ("type", wintypes.DWORD),
        ("_anon", _INPUT_UNION),
    )


_user32.SendInput.restype = wintypes.UINT
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def paste_via_sendinput() -> None:
    """用 SendInput 模拟 Ctrl 按下 -> V 按下 -> V 松开 -> Ctrl 松开 (4 个 INPUT)。

    失败 (SendInput 返回值 < 提交数量，通常意味着被 UIPI 阻止) 抛 OutputError。
    成功不代表目标窗口一定接受了粘贴 (目标可能无焦点或非编辑控件)，只表示按键事件已注入。
    """
    inputs = (_INPUT * 4)()

    # 0: Ctrl 按下
    inputs[0].type = _INPUT_KEYBOARD
    inputs[0].ki.wVk = _VK_CONTROL
    inputs[0].ki.dwFlags = 0
    # 1: V 按下
    inputs[1].type = _INPUT_KEYBOARD
    inputs[1].ki.wVk = _VK_V
    inputs[1].ki.dwFlags = 0
    # 2: V 松开
    inputs[2].type = _INPUT_KEYBOARD
    inputs[2].ki.wVk = _VK_V
    inputs[2].ki.dwFlags = _KEYEVENTF_KEYUP
    # 3: Ctrl 松开
    inputs[3].type = _INPUT_KEYBOARD
    inputs[3].ki.wVk = _VK_CONTROL
    inputs[3].ki.dwFlags = _KEYEVENTF_KEYUP

    sent = _user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
    if sent != 4:
        err = ctypes.get_last_error()
        # 常见原因：目标窗口完整性级别更高 (UIPI 阻止注入)。
        raise OutputError(
            f"SendInput 未完全成功，已注入 {sent}/4，GetLastError={err}"
        )


def paste_text(text: str) -> bool:
    """组合流程：写剪贴板 -> 短暂 sleep -> 模拟 Ctrl+V 粘贴。

    返回值语义：
    - True: SendInput 注入成功 (剪贴板已写入)。
    - False: SendInput 失败 (如目标窗口高完整性级别)；此时剪贴板内容必须保留
      (不主动清空)，由上层提示用户手动 Ctrl+V。
    - set_clipboard_text 自身失败直接抛 OutputError (向上传播，不返回 False)。
    """
    # 第一步：写剪贴板。失败直接抛 OutputError，符合契约。
    set_clipboard_text(text)

    # 短暂等待，确保剪贴板写入对随后注入的 Ctrl+V 可见。
    # sleep 在 worker 线程中执行，不在 UI 主线程，符合 AGENTS.md。
    time.sleep(0.05)

    try:
        paste_via_sendinput()
    except OutputError:
        # 注入失败：保留剪贴板内容，返回 False 让上层提示手动粘贴。
        logger.warning("[paste_text] SendInput 注入失败，剪贴板内容已保留", exc_info=True)
        return False
    return True
