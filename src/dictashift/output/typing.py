"""通过 Win32 SendInput 进行 Unicode 逐字符直输 (不经过剪贴板)。

不变量 (AGENTS.md 5.5)：
- 默认输出流程不再是"写剪贴板 -> 模拟 Ctrl+V"；而是直接 SendInput Unicode。
- 无法注入到高完整性级别窗口或无聚焦 caret 时，type_text_at_cursor 返回 False，
  由上层弹窗展示文本并由用户决定后续操作。
- 不主动覆盖系统剪贴板。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from .caret import has_focused_caret
from .clipboard import OutputError

logger = logging.getLogger(__name__)


# ==================== Win32 常量 ====================

_INPUT_KEYBOARD = 1
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002

# 每次 SendInput 提交的字符数（每字符 2 个 INPUT：按下 + 抬起）。
BATCH_SIZE = 24

# ==================== SendInput 结构与函数原型 ====================

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),  # ULONG_PTR
    )


class _INPUT_UNION(ctypes.Union):
    # Win32 INPUT 的 union 最大成员是 MOUSEINPUT（64 位下 32 字节），
    # 因此 padding 必须按 32 字节补齐：sizeof(INPUT) = type 4 + 对齐 4 + union 32 = 40。
    # SendInput 要求 cbSize 严格等于 sizeof(INPUT)=40，
    # 否则返回 0 且 GetLastError=87 (ERROR_INVALID_PARAMETER)。
    _fields_ = (("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32))


class INPUT(ctypes.Structure):
    _anonymous_ = ("_anon",)
    _fields_ = (
        ("type", wintypes.DWORD),
        ("_anon", _INPUT_UNION),
    )


_user32.SendInput.restype = wintypes.UINT
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


# ==================== 纯函数 ====================


def strip_emoji(text: str) -> str:
    """过滤 emoji：全部非 BMP 字符（U+10000 及以上）、BMP 表情符号区
    （U+2600-U+27BF、U+2B00-U+2BFF、U+FE00-U+FE0F 变体选择符、U+200D 零宽连接符、
    U+20E3 组合 enclosed keycap）。普通中英文/标点/数字/常用符号（如 ©®™）保留。
    ©(U+00A9) 和 ®(U+00AE) 后的 FE0F 变体选择符被过滤不影响显示。"""

    def _is_emoji(cp: int) -> bool:
        # 全部非 BMP（U+10000 及以上），包括所有 emoji（表情、补充符号、旗帜等）。
        if cp >= 0x10000:
            return True
        # BMP 表情/符号区。
        if 0x2600 <= cp <= 0x27BF:      # 杂项符号 + 装饰符号（☀ ⛄ ✂ ✏ ✅ 等）
            return True
        if 0x2B00 <= cp <= 0x2BFF:      # 其他符号（⬛ ⬤ 等）
            return True
        if 0xFE00 <= cp <= 0xFE0F:      # 变体选择符 VS1-VS16
            return True
        if cp == 0x200D:                # 零宽连接符 ZWJ（用于 emoji 组合）
            return True
        if cp == 0x20E3:                # 组合 enclosed keycap（数字键帽 emoji 的组合符）
            return True
        return False

    if not text:
        return text
    return "".join(ch for ch in text if not _is_emoji(ord(ch)))


def _char_to_utf16_units(ch: str) -> list[int]:
    """单个字符 -> UTF-16 code unit 列表 (BMP 字符 1 个，非 BMP surrogate pair 2 个)。"""
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def text_to_units(text: str) -> list[int]:
    """把文本转换为 UTF-16 code unit 序列 (KEYEVENTF_UNICODE 的 wScan 值)。

    - 换行符 \\n 和 \\r 替换为空格 (语音输入不产生多行文本；聊天框里 Return 会触发发送)。
    - 非 BMP 字符 (如 emoji) 转换为 surrogate pair 两个 unit。

    返回 list[int]，每个元素是 0x0000..0xFFFF 的 16 位 code unit。
    """
    units: list[int] = []
    for ch in text:
        if ch == "\n" or ch == "\r":
            units.append(ord(" "))
            continue
        units.extend(_char_to_utf16_units(ch))
    return units


# ==================== 注入实现 ====================


def type_text(text: str) -> None:
    """逐字符 Unicode 直输。失败抛 OutputError。

    流程：text_to_units -> 按 BATCH_SIZE 分批 -> 每批构造 INPUT 数组
    (每字符 2 个 INPUT：wScan=unit, dwFlags=KEYEVENTF_UNICODE / KEYEVENTF_UNICODE|KEYEVENTF_KEYUP)
    -> SendInput。任一批 sent != 提交数 抛 OutputError (UIPI 拦截等)。
    """
    units = text_to_units(text)
    if not units:
        return

    for start in range(0, len(units), BATCH_SIZE):
        batch = units[start : start + BATCH_SIZE]
        inputs = (INPUT * (len(batch) * 2))()
        for i, unit in enumerate(batch):
            down = inputs[i * 2]
            up = inputs[i * 2 + 1]
            down.type = _INPUT_KEYBOARD
            down.ki.wVk = 0
            down.ki.wScan = unit
            down.ki.dwFlags = _KEYEVENTF_UNICODE
            up.type = _INPUT_KEYBOARD
            up.ki.wVk = 0
            up.ki.wScan = unit
            up.ki.dwFlags = _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP

        # 数组本身即可衰减为 POINTER(INPUT)；byref(数组) 会得到
        # "pointer to INPUT_Array_*"，与 argtypes 声明不符而抛 ArgumentError。
        sent = _user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            err = ctypes.get_last_error()
            # 常见原因：目标窗口完整性级别更高 (UIPI 阻止注入)。
            raise OutputError(
                f"SendInput 未完全成功，已注入 {sent}/{len(inputs)}，GetLastError={err}"
            )


def type_text_at_cursor(text: str) -> bool:
    """对外主入口 (controller 默认 paste_func)。

    返回值语义与原 paste_text 对齐：True=已输出，False=失败需上层处理。
    - 空文本直接返回 True。
    - has_focused_caret() 为 False -> 返回 False (不尝试注入，由上层弹窗)。
    - 过滤 emoji 后为空串 -> 视为已处理，记 info 日志并返回 True（不注入）。
    - type_text 抛 OutputError -> 返回 False (记 warning 日志)。
    """
    if not text:
        return True
    has_caret = has_focused_caret()
    logger.info(
        "[type_text_at_cursor] has_focused_caret=%s, 文本长度=%d", has_caret, len(text)
    )
    if not has_caret:
        return False
    # 过滤 emoji（产品决策：Qt 目标 emoji 错位，默认过滤）。
    # 注意：controller.on_text_ready 仍携带原文，弹窗可显示完整内容。
    filtered = strip_emoji(text)
    if not filtered:
        logger.info("[type_text_at_cursor] emoji 过滤后为空串，不注入（原文 %d 字符）", len(text))
        return True
    try:
        type_text(filtered)
    except OutputError:
        logger.warning("[type_text_at_cursor] 直输失败，请查看弹窗", exc_info=True)
        return False
    return True
