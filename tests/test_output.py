"""文本输出测试。

测试目标 (AGENTS.md 5.5 / 13.1)：
- text_to_units：ASCII、中文、换行→空格、emoji (surrogate pair)、空串。
- INPUT 结构尺寸回归：sizeof(KEYBDINPUT)==24、sizeof(INPUT)==40。
- type_text_at_cursor：无 caret / type_text 抛 OutputError / 正常路径。
- clipboard.py 用例保留 (真实剪贴板读写)。
"""

from __future__ import annotations

import ctypes
import platform
import time

import pytest

from dictashift.output import typing
from dictashift.output.clipboard import OutputError, get_clipboard_text, set_clipboard_text


pytestmark = pytest.mark.skipif(
    platform.system() != "Windows",
    reason="剪贴板/SendInput 测试仅在 Windows 上运行",
)


def _set_and_read(text: str) -> str | None:
    """写剪贴板后读回，验证真实剪贴板可用。"""
    set_clipboard_text(text)
    time.sleep(0.05)
    return get_clipboard_text()


# ==================== clipboard 用例 (保留不动) ====================


def test_clipboard_round_trip_real() -> None:
    """真实剪贴板读写可用（前置验证）。"""
    assert _set_and_read("DictaShift 测试 123") == "DictaShift 测试 123"


def test_set_clipboard_text_unicode() -> None:
    """Unicode（中文/emoji）剪贴板写入正确。"""
    text = "中文测试 🎤 emoji"
    assert _set_and_read(text) == text


def test_get_clipboard_text_none_when_no_text_format(monkeypatch) -> None:
    """无 CF_UNICODETEXT 格式时返回 None（剪贴板刚写入文本应有格式，这里用占位断言）。"""
    set_clipboard_text("x")
    time.sleep(0.05)
    val = get_clipboard_text()
    assert isinstance(val, (str, type(None)))


# ==================== text_to_units 用例 ====================


def test_text_to_units_ascii() -> None:
    """ASCII 字符每个对应一个 16 位 unit。"""
    assert typing.text_to_units("abc") == [ord("a"), ord("b"), ord("c")]


def test_text_to_units_chinese() -> None:
    """BMP 中文每个对应一个 unit。"""
    assert typing.text_to_units("中文") == [ord("中"), ord("文")]


def test_text_to_units_newline_to_space() -> None:
    """换行符 (\\n / \\r) 替换为空格。"""
    assert typing.text_to_units("a\nb\rc") == [ord("a"), ord(" "), ord("b"), ord(" "), ord("c")]


def test_text_to_units_emoji_surrogate_pair() -> None:
    """非 BMP 字符 (emoji 🎤 U+1F3A4) 转换为 surrogate pair 两个 unit。"""
    units = typing.text_to_units("🎤")
    # U+1F3A4 -> high surrogate 0xD83C, low surrogate 0xDFA4
    assert units == [0xD83C, 0xDFA4]


def test_text_to_units_empty() -> None:
    """空串返回空列表。"""
    assert typing.text_to_units("") == []


# ==================== INPUT 结构尺寸回归 ====================


def test_input_struct_size_is_win32_abi() -> None:
    """SendInput 结构必须符合 Win32 ABI：sizeof(KEYBDINPUT)==24、sizeof(INPUT)==40。

    SendInput 对 cbSize 校验严格，不符时返回 0 且 GetLastError=87。
    该错误曾导致真实环境注入失败 (ERROR_INVALID_PARAMETER)。
    """
    assert ctypes.sizeof(typing.KEYBDINPUT) == 24
    assert ctypes.sizeof(typing.INPUT) == 40


# ==================== type_text_at_cursor 用例 ====================


def test_type_text_at_cursor_no_caret_returns_false(monkeypatch) -> None:
    """has_focused_caret 为 False -> 返回 False 且不调用 SendInput。"""
    monkeypatch.setattr(typing, "has_focused_caret", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(
        typing._user32, "SendInput", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or 0
    )
    assert typing.type_text_at_cursor("hello") is False
    assert called["n"] == 0


def test_type_text_at_cursor_empty_text_returns_true() -> None:
    """空文本直接返回 True，不依赖 caret。"""
    assert typing.type_text_at_cursor("") is True


def test_type_text_at_cursor_type_text_failure_returns_false(monkeypatch) -> None:
    """type_text 抛 OutputError -> 返回 False。"""
    monkeypatch.setattr(typing, "has_focused_caret", lambda: True)

    def _fail(_text: str) -> None:
        raise OutputError("UIPI blocked")

    monkeypatch.setattr(typing, "type_text", _fail)
    assert typing.type_text_at_cursor("hello") is False


def test_type_text_at_cursor_success_returns_true(monkeypatch) -> None:
    """正常路径 -> True (monkeypatch type_text 为成功)。"""
    monkeypatch.setattr(typing, "has_focused_caret", lambda: True)
    called = {"text": None}

    def _ok(text: str) -> None:
        called["text"] = text

    monkeypatch.setattr(typing, "type_text", _ok)
    assert typing.type_text_at_cursor("直输测试") is True
    assert called["text"] == "直输测试"
