"""划词取词编排：自动复制检测 -> (非终端) 模拟 Ctrl+C -> 读剪贴板 -> 全格式还原。

不变量：
- 前台窗口属于本进程时不取词 (skipped_self)，避免模拟按键打到自己。
- 终端类窗口 (Windows Terminal / conhost / mintty 等) 永不模拟 Ctrl+C：
  终端在无选区时把 Ctrl+C 解释为 SIGINT，会杀掉用户前台运行的程序；
  且这类终端普遍「划选即复制」，优先靠按压锚定检测自动复制取词。
- 按压锚定：左键按下时记录剪贴板序列号 (press_seq)，取词先等 auto_copy_wait_s；
  序列号已变化说明终端/应用已自动复制 (或用户自己按了 Ctrl+C)，直接读文本，
  不备份、不模拟按键、不还原 (剪贴板是用户自己复制的内容，还原反而破坏其习惯)。
- 剪贴板序列号未变化 (无选区 / 复制失败) 时不做还原——剪贴板根本没动过。
- 只要备份完成后进入过取词流程，任何路径 (成功 / 无文本 / 超长 / 异常)
  都必须把用户剪贴板原样还原；异常不向外抛，归入 error 结果。
- 回调线程安全：纯 Win32 ctypes，不依赖 Qt。

可测试性：GetClipboardSequenceNumber 等 Win32 调用走模块级 _user32，
simulate_copy / get_clipboard_text / backup_clipboard / _foreground_pid /
_foreground_hwnd / _window_class_name 均为模块级名字，测试直接 monkeypatch。
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum

from dictashift.output.clipboard import OutputError, get_clipboard_text
from dictashift.output.typing import INPUT

from .clipboard_backup import ClipboardSnapshot, backup_clipboard

logger = logging.getLogger(__name__)


# ==================== Win32 常量与原型 ====================

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_VK_CONTROL = 0x11
_VK_C = 0x43

_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]

_user32.SendInput.restype = wintypes.UINT
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.GetClipboardSequenceNumber.argtypes = []
# GetClassNameW：取窗口类名 (终端判定用)。原型 int WINAPI GetClassNameW(HWND, LPWSTR, int)。
_user32.GetClassNameW.restype = wintypes.INT
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]


# 已知终端窗口类名集合。终端在无选区时把 Ctrl+C 解释为 SIGINT (杀掉前台进程)，
# 且普遍支持「划选即复制」，故对终端永不模拟按键，只依赖自动复制检测
# (见 capture_selection)。
_TERMINAL_WINDOW_CLASSES = frozenset(
    {
        "ConsoleWindowClass",  # conhost (cmd / 旧版 PowerShell)
        "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
        "mintty",  # Git Bash / Cygwin / MSYS2
        "PuTTY",
        "Alacritty",
        "org.wezfurlong.wezterm",  # WezTerm
    }
)

# 小写形式 (Win32 类名大小写不敏感，比较统一走小写，同 output.caret 的约定)。
_TERMINAL_WINDOW_CLASSES_LOWER = frozenset(c.lower() for c in _TERMINAL_WINDOW_CLASSES)


# ==================== 结果类型 ====================


class CaptureStatus(Enum):
    """取词结果分类，供上层决定是否提示用户。"""

    SUCCESS = "success"  # 取到文本
    NO_TEXT = "no_text"  # 无选区 / 剪贴板无文本
    TOO_LONG = "too_long"  # 文本超过 max_chars
    SKIPPED_SELF = "skipped_self"  # 前台是本进程，主动跳过
    ERROR = "error"  # 异常 (已尽力还原剪贴板)


@dataclass(frozen=True)
class CaptureResult:
    status: CaptureStatus
    text: str | None = None  # 仅 SUCCESS 携带
    reason: str = ""  # 简短原因串，供上层提示 / 日志
    length: int = 0  # 实际文本长度 (TOO_LONG 时携带)

    @property
    def ok(self) -> bool:
        return self.status is CaptureStatus.SUCCESS


# ==================== 取词实现 ====================


def simulate_copy() -> None:
    """SendInput 发送 Ctrl+C (按下 Ctrl -> 按下 C -> 抬起 C -> 抬起 Ctrl)。

    复用 output.typing.INPUT 结构 (含 64 位 padding，sizeof(INPUT)=40)。
    未完全注入抛 OutputError (常见原因：目标窗口完整性级别更高，UIPI 拦截)。
    """
    sequence = (
        (_VK_CONTROL, 0),
        (_VK_C, 0),
        (_VK_C, _KEYEVENTF_KEYUP),
        (_VK_CONTROL, _KEYEVENTF_KEYUP),
    )
    inputs = (INPUT * len(sequence))()
    for i, (vk, flags) in enumerate(sequence):
        inputs[i].type = _INPUT_KEYBOARD
        inputs[i].ki.wVk = vk
        inputs[i].ki.wScan = 0
        inputs[i].ki.dwFlags = flags

    sent = _user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        err = ctypes.get_last_error()
        raise OutputError(
            f"SendInput 模拟 Ctrl+C 未完全成功，已注入 {sent}/{len(inputs)}，GetLastError={err}"
        )


def get_clipboard_seq() -> int:
    """当前剪贴板序列号 (GetClipboardSequenceNumber)。

    微秒级纯 Win32 调用，可在 pynput 回调线程同步取样，
    用作「划选即复制」自动复制检测的按压锚点。
    """
    return _user32.GetClipboardSequenceNumber()


def _foreground_hwnd() -> int | None:
    """前台窗口句柄；无前台窗口返回 None。"""
    hwnd = _user32.GetForegroundWindow()
    return hwnd or None


def _foreground_pid() -> int | None:
    """前台窗口所属进程 PID；无前台窗口或 API 失败返回 None。"""
    hwnd = _foreground_hwnd()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    if not _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return None
    return pid.value or None


def _window_class_name(hwnd: int | None) -> str | None:
    """GetClassNameW 取窗口类名；空 hwnd 或 API 失败返回 None。

    调用方对 None 按「未知」处理 (与 _foreground_pid 放行 None 的策略一致)。
    """
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(256)
    got = _user32.GetClassNameW(hwnd, buf, len(buf))
    if not got:
        return None
    return buf.value


def _is_terminal_class(class_name: str | None) -> bool:
    """窗口类名是否命中已知终端集合 (大小写不敏感)。

    取不到类名 (None / 空串) 按非终端处理，保持乐观：
    与 _foreground_pid 放行 None 的策略一致。
    """
    if not class_name:
        return False
    return class_name.lower() in _TERMINAL_WINDOW_CLASSES_LOWER


def _wait_for_sequence_change(seq_before: int, timeout_s: float, poll_interval_s: float) -> bool:
    """轮询剪贴板序列号直到变化或超时。True=已变化。"""
    deadline = time.monotonic() + timeout_s
    while True:
        if _user32.GetClipboardSequenceNumber() != seq_before:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_s)


def _restore_quietly(snapshot: ClipboardSnapshot) -> None:
    """异常路径上的尽力还原；失败只记日志 (主错误优先)。"""
    try:
        snapshot.restore()
    except Exception:  # noqa: BLE001 - 还原失败不得遮蔽主异常
        logger.warning("[capture_selection] 剪贴板还原失败", exc_info=True)


def _classify_text(text: str | None, max_chars: int) -> CaptureResult:
    """把读到的剪贴板文本归类为 no_text / too_long / success。"""
    if text is None or not text.strip():
        return CaptureResult(CaptureStatus.NO_TEXT, reason="剪贴板无文本内容")
    if len(text) > max_chars:
        return CaptureResult(
            CaptureStatus.TOO_LONG,
            reason=f"选中文本过长 ({len(text)} > {max_chars} 字符)",
            length=len(text),
        )
    return CaptureResult(CaptureStatus.SUCCESS, text=text, length=len(text))


def capture_selection(
    max_chars: int = 2000,
    wait_timeout_s: float = 0.5,
    poll_interval_s: float = 0.03,
    press_seq: int | None = None,
    auto_copy_wait_s: float = 0.3,
) -> CaptureResult:
    """取当前选区文本：优先靠「划选即复制」自动复制，非终端才模拟 Ctrl+C。

    流程：本进程守卫 -> 终端窗口判定 -> 按压锚定自动复制检测 (press_seq) ->
    终端放弃 (no_text) / 非终端全格式备份 -> simulate_copy -> 轮询序列号 ->
    读文本 -> 还原 -> 分类结果。自动复制已取到文本时不备份/不按键/不还原
    (剪贴板是用户自己复制的内容)。任何异常路径归入 ERROR 结果，不向外抛。
    """
    try:
        # 1. 守卫：前台窗口是本进程 (自己的弹窗/设置窗) 时不取词。
        if _foreground_pid() == os.getpid():
            return CaptureResult(CaptureStatus.SKIPPED_SELF, reason="前台窗口属于本进程，跳过取词")

        # 2. 终端判定：终端无选区时 Ctrl+C = SIGINT，会杀掉用户前台进程，
        #    故终端窗口永不模拟按键。取不到类名 (API 失败) 按非终端处理 (保持乐观)。
        is_terminal = _is_terminal_class(_window_class_name(_foreground_hwnd()))

        # 3. 按压锚定：左键按下时的序列号与当前不同 -> 终端/应用已「划选即复制」
        #    (或用户自己按了 Ctrl+C)，直接读文本，不备份、不模拟按键、不还原。
        if press_seq is not None and _wait_for_sequence_change(
            press_seq, auto_copy_wait_s, poll_interval_s
        ):
            return _classify_text(get_clipboard_text(), max_chars)

        # 4. 终端兜底：自动复制检测未取到文本，绝不模拟 Ctrl+C，放弃本次取词。
        if is_terminal:
            return CaptureResult(
                CaptureStatus.NO_TEXT, reason="终端窗口，等待自动复制（未模拟 Ctrl+C）"
            )

        # 5. 非终端：记录当前剪贴板序列号，用于判断 Ctrl+C 是否真正改变了剪贴板。
        #    注意此处重新采样：press_seq 检测窗口与 simulate 后的等待是两个阶段，
        #    不能混用 press_seq 作为 seq_before。
        seq_before = _user32.GetClipboardSequenceNumber()

        # 6. 全格式备份 (取词后必须原样放回)。
        snapshot = backup_clipboard()
    except Exception as exc:  # noqa: BLE001 - 统一归入 error 结果
        logger.exception("[capture_selection] 取词前置步骤失败")
        return CaptureResult(CaptureStatus.ERROR, reason=f"{type(exc).__name__}: {exc}")

    try:
        # 7. 模拟 Ctrl+C 并等待剪贴板变化。
        simulate_copy()
        if not _wait_for_sequence_change(seq_before, wait_timeout_s, poll_interval_s):
            # 序列号未变：无选区或复制失败，剪贴板没被碰过，不做还原。
            return CaptureResult(CaptureStatus.NO_TEXT, reason="剪贴板未变化（无选区或复制失败）")

        # 8. 读新剪贴板文本。
        text = get_clipboard_text()
    except Exception as exc:  # noqa: BLE001 - 异常路径必须完成还原
        logger.exception("[capture_selection] 取词过程异常")
        _restore_quietly(snapshot)
        return CaptureResult(CaptureStatus.ERROR, reason=f"{type(exc).__name__}: {exc}")

    # 9. 正常路径：先还原用户剪贴板，再分类文本结果。
    try:
        snapshot.restore()
    except Exception as exc:  # noqa: BLE001 - 还原失败归入 error 结果
        logger.exception("[capture_selection] 剪贴板还原失败")
        return CaptureResult(CaptureStatus.ERROR, reason=f"剪贴板还原失败: {type(exc).__name__}: {exc}")

    return _classify_text(text, max_chars)
