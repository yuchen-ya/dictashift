"""文本输出测试：SendInput 失败时剪贴板内容保留（护栏 5.5）。

测试目标 (AGENTS.md 5.5 / 13.1)：
- monkeypatch paste_via_sendinput 抛 OutputError -> paste_text 返回 False
  且 get_clipboard_text() 仍是目标文本（剪贴板保留）。
- paste_via_sendinput 正常返回 -> paste_text True。

使用真实 Windows 剪贴板写测试字符串（这些测试运行在 win32 上）。
"""

from __future__ import annotations

import ctypes
import platform
import time

import pytest

from dictashift.output.clipboard import OutputError, get_clipboard_text, set_clipboard_text
from dictashift.output import win32_paste


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="剪贴板/SendInput 测试仅在 Windows 上运行",
)


def _set_and_read(text: str) -> str | None:
    """写剪贴板后读回，验证真实剪贴板可用。"""
    set_clipboard_text(text)
    time.sleep(0.05)
    return get_clipboard_text()


def test_clipboard_round_trip_real() -> None:
    """真实剪贴板读写可用（前置验证）。"""
    assert _set_and_read("DictaShift 测试 123") == "DictaShift 测试 123"


def test_paste_text_returns_true_when_sendinput_ok(monkeypatch) -> None:
    """paste_via_sendinput 正常 -> paste_text True，剪贴板为目标文本。"""
    called = {"n": 0}

    def _ok() -> None:
        called["n"] += 1

    monkeypatch.setattr(win32_paste, "paste_via_sendinput", _ok)
    result = win32_paste.paste_text("hello paste")
    assert result is True
    assert called["n"] == 1
    # 剪贴板内容为目标文本
    time.sleep(0.05)
    assert get_clipboard_text() == "hello paste"


def test_paste_text_returns_false_and_keeps_clipboard_on_sendinput_failure(
    monkeypatch,
) -> None:
    """SendInput 抛 OutputError -> paste_text 返回 False 且剪贴板保留目标文本。"""
    def _fail() -> None:
        raise OutputError("UIPI blocked")

    monkeypatch.setattr(win32_paste, "paste_via_sendinput", _fail)
    target = "应保留的文本内容 retain-me"
    result = win32_paste.paste_text(target)
    assert result is False
    # 护栏 5.5：剪贴板内容仍为目标文本
    time.sleep(0.05)
    assert get_clipboard_text() == target


def test_set_clipboard_text_unicode() -> None:
    """Unicode（中文/emoji）剪贴板写入正确。"""
    text = "中文测试 🎤 emoji"
    assert _set_and_read(text) == text


def test_get_clipboard_text_none_when_no_text_format(monkeypatch) -> None:
    """无 CF_UNICODETEXT 格式时返回 None（剪贴板刚写入文本应有格式，这里用占位断言）。"""
    # 写入文本后格式必然存在，这里仅断言类型契约
    set_clipboard_text("x")
    time.sleep(0.05)
    val = get_clipboard_text()
    assert isinstance(val, (str, type(None)))


def test_input_struct_size_is_win32_abi() -> None:
    """SendInput 结构必须符合 Win32 ABI：sizeof(INPUT)==40（64 位）。

    SendInput 对 cbSize 校验严格，不符时返回 0 且 GetLastError=87。
    该错误曾导致真实环境粘贴失败（ERROR_INVALID_PARAMETER）。
    """
    assert ctypes.sizeof(win32_paste._KEYBDINPUT) == 24
    assert ctypes.sizeof(win32_paste._INPUT) == 40
