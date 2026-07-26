"""caret.has_focused_caret 的 shell 外壳黑名单测试。

覆盖：
- _is_non_editable_shell_target 纯函数（黑名单命中 / 大小写不敏感 / 正常编辑器类名不命中 / 空串）。
- has_focused_caret 在前台窗口或聚焦控件命中黑名单时返回 False（即便 hwndFocus 非空、
  有 caret 标志）。
- GetClassNameW 取类名失败时不误判（保持原有乐观判据，仍返回 True）。
- 正常编辑器类名（Edit/QtWidget）不命中黑名单 -> True。

Win32 集成部分通过 monkeypatch _user32 的函数构造场景，可在安装了 PySide6 的
Windows / offscreen 环境运行；纯函数部分不依赖平台。
"""

from __future__ import annotations

import ctypes
import platform

import pytest


# ==================== 纯函数 _is_non_editable_shell_target ====================


@pytest.mark.parametrize(
    "class_name",
    [
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        "TaskSwitcherWnd",
    ],
)
def test_blacklist_class_names_match(class_name: str) -> None:
    from dictashift.output.caret import _is_non_editable_shell_target

    assert _is_non_editable_shell_target(class_name) is True


@pytest.mark.parametrize(
    "class_name",
    ["progman", "WORKERW", "shell_traywnd", "Shell_SecondaryTrayWnd".lower()],
)
def test_blacklist_case_insensitive(class_name: str) -> None:
    """Win32 类名比较大小写不敏感。"""
    from dictashift.output.caret import _is_non_editable_shell_target

    assert _is_non_editable_shell_target(class_name) is True


@pytest.mark.parametrize(
    "class_name",
    [
        "Edit",            # 标准 Win32 编辑控件
        "RichEdit20W",     # RichEdit
        "Qt5152QWindowIcon",
        "Chrome_WidgetWin_1",
        "Notepad",
        "ComboBox",
    ],
)
def test_normal_editor_classes_not_blacklisted(class_name: str) -> None:
    """正常编辑器 / 应用窗口类名不应命中黑名单。"""
    from dictashift.output.caret import _is_non_editable_shell_target

    assert _is_non_editable_shell_target(class_name) is False


def test_empty_string_not_blacklisted() -> None:
    from dictashift.output.caret import _is_non_editable_shell_target

    assert _is_non_editable_shell_target("") is False


# ==================== has_focused_caret 与黑名单集成（Win32 monkeypatch） ====================


pytestmark_int = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="has_focused_caret 集成测试仅在 Windows 上运行（依赖 _user32）",
)


def _patch_thread_chain(monkeypatch, write_info=None, gui_thread_info_ret: int = 1) -> None:
    """patch caret 模块的 GetForegroundWindow/GetWindowThreadProcessId/GetGUIThreadInfo。

    write_info(structure): 可选，接收已 cast 出来的 _GUITHREADINFO 实例。
    """
    from dictashift.output import caret

    monkeypatch.setattr(caret._user32, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(
        caret._user32, "GetWindowThreadProcessId", lambda hwnd, pid_ptr: 6789
    )

    def _ggi(thread_id, info_ptr):
        if write_info is not None:
            casted = ctypes.cast(info_ptr, ctypes.POINTER(caret._GUITHREADINFO))
            write_info(casted.contents)
        return gui_thread_info_ret

    monkeypatch.setattr(caret._user32, "GetGUIThreadInfo", _ggi)


def _patch_get_class_name(monkeypatch, class_map: dict) -> None:
    """patch GetClassNameW：按 hwnd 返回 class_map 中的类名；未配置的返回 0（失败）。

    class_map: {hwnd_int: class_name_str}。
    """
    from dictashift.output import caret

    def _gcn(hwnd, buf, n):
        name = class_map.get(hwnd)
        if not name:
            return 0
        # 写入 unicode buffer（截断到 n-1）。
        truncated = name[: max(0, n - 1)]
        for i, ch in enumerate(truncated):
            buf[i] = ch
        buf[len(truncated)] = "\x00"
        return len(truncated)

    monkeypatch.setattr(caret._user32, "GetClassNameW", _gcn)


@pytestmark_int
def test_has_focused_caret_false_when_foreground_is_shell(monkeypatch) -> None:
    """前台窗口类名命中黑名单（Progman 桌面）-> False，即便 hwndFocus 非空有 caret。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 9999
        info.flags = caret._GUI_CARETBLINKING  # 原本会判定 True

    _patch_thread_chain(monkeypatch, write_info=_write)
    # 前台 hwnd=12345 -> Progman；聚焦控件 hwnd=9999 -> 未配置（GetClassNameW 失败，忽略）。
    _patch_get_class_name(monkeypatch, {12345: "Progman"})

    assert caret.has_focused_caret() is False


@pytestmark_int
def test_has_focused_caret_false_when_focus_target_is_shell(monkeypatch) -> None:
    """前台窗口正常，但聚焦控件类名命中黑名单（Shell_TrayWnd 任务栏）-> False。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 9999
        info.flags = caret._GUI_CARETBLINKING

    _patch_thread_chain(monkeypatch, write_info=_write)
    _patch_get_class_name(
        monkeypatch,
        {12345: "Notepad", 9999: "Shell_TrayWnd"},
    )

    assert caret.has_focused_caret() is False


@pytestmark_int
def test_has_focused_caret_true_when_neither_is_shell(monkeypatch) -> None:
    """前台与聚焦控件均为正常编辑器类名 -> 维持乐观判据 True。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 9999
        info.flags = caret._GUI_CARETBLINKING

    _patch_thread_chain(monkeypatch, write_info=_write)
    _patch_get_class_name(
        monkeypatch,
        {12345: "Notepad", 9999: "Edit"},
    )

    assert caret.has_focused_caret() is True


@pytestmark_int
def test_has_focused_caret_true_when_getclassname_fails(monkeypatch) -> None:
    """GetClassNameW 失败（返回 0）-> 忽略黑名单检查，维持原有乐观判据 True。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 9999
        info.flags = caret._GUI_CARETBLINKING

    _patch_thread_chain(monkeypatch, write_info=_write)
    # 两个 hwnd 都未配置 -> _gcn 返回 0 -> _get_class_name 返回 None -> 忽略。
    _patch_get_class_name(monkeypatch, {})

    assert caret.has_focused_caret() is True


@pytestmark_int
def test_has_focused_caret_blacklist_case_insensitive_in_integration(monkeypatch) -> None:
    """集成层大小写不敏感：'progman'（小写）也应命中。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 9999
        info.flags = caret._GUI_CARETBLINKING

    _patch_thread_chain(monkeypatch, write_info=_write)
    _patch_get_class_name(monkeypatch, {12345: "progman"})

    assert caret.has_focused_caret() is False
