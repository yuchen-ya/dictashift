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


# ==================== text_to_units：混合文本补充 ====================


def test_text_to_units_mixed_text() -> None:
    """混合 ASCII + 中文 + emoji + 换行（转空格）按 UTF-16 顺序拼接。

    "a中\n🎤" -> [ord('a'), ord('中'), ord(' '), 0xD83C, 0xDFA4]
    """
    units = typing.text_to_units("a中\n🎤")
    assert units == [ord("a"), ord("中"), ord(" "), 0xD83C, 0xDFA4]


# ==================== type_text 分批逻辑 ====================


def test_type_text_batches_multiple_sendinput_calls(monkeypatch) -> None:
    """超过 BATCH_SIZE 的文本触发多次 SendInput；每字符 2 个 INPUT；
    最后一批数量正确；每次 cbSize==sizeof(INPUT)==40。"""
    # 30 个 ASCII 字符 = 30 个 unit（BMP，每字符 1 unit）。
    # BATCH_SIZE=24 -> 第 1 批 24 unit（48 INPUT），第 2 批 6 unit（12 INPUT）。
    text = "a" * 30
    calls: list[tuple[int, int]] = []  # (n_inputs, cbSize)

    def _fake_send_input(n_inputs, inputs, cb_size):
        calls.append((n_inputs, cb_size))
        return n_inputs  # 全部成功

    monkeypatch.setattr(typing._user32, "SendInput", _fake_send_input)
    typing.type_text(text)

    # 应有两次调用
    assert len(calls) == 2
    # 每次提交的 INPUT 数与文本字符数匹配（每字符 2 个 INPUT）
    assert calls[0][0] == typing.BATCH_SIZE * 2  # 24 * 2 = 48
    assert calls[1][0] == (30 - typing.BATCH_SIZE) * 2  # 6 * 2 = 12
    # cbSize 必须严格等于 sizeof(INPUT)=40（SendInput 校验依赖）
    for _n, cb in calls:
        assert cb == ctypes.sizeof(typing.INPUT)
        assert cb == 40


def test_type_text_short_single_sendinput(monkeypatch) -> None:
    """短文本（<=BATCH_SIZE）只触发一次 SendInput。"""
    calls: list[int] = []

    def _fake_send_input(n_inputs, inputs, cb_size):
        calls.append(n_inputs)
        return n_inputs

    monkeypatch.setattr(typing._user32, "SendInput", _fake_send_input)
    typing.type_text("hi")  # 2 个 unit -> 1 次调用，4 个 INPUT
    assert len(calls) == 1
    assert calls[0] == 4  # 2 字符 * 2 INPUT


def test_type_text_empty_does_not_call_sendinput(monkeypatch) -> None:
    """空文本不触发 SendInput。"""
    called = {"n": 0}

    def _fake_send_input(*a, **kw):
        called["n"] += 1
        return 0

    monkeypatch.setattr(typing._user32, "SendInput", _fake_send_input)
    typing.type_text("")
    assert called["n"] == 0


def test_type_text_partial_send_raises_outputerror(monkeypatch) -> None:
    """SendInput 返回数 < 提交数（UIPI 拦截等）-> OutputError。"""

    def _fake_send_input(n_inputs, inputs, cb_size):
        return n_inputs - 1  # 少注入 1 个

    monkeypatch.setattr(typing._user32, "SendInput", _fake_send_input)
    with pytest.raises(OutputError):
        typing.type_text("abc")


# ==================== caret.has_focused_caret 路径 ====================

# 注意：has_focused_caret 在函数内部创建 _GUITHREADINFO 实例，无法从外部
# 注入；通过 monkeypatch _user32 的三个 Win32 函数构造场景。
# GetGUIThreadInfo 的被调用签名是 (thread_id, ctypes.byref(info))，
# patched 函数接收到的第二个参数是 ctypes 指针，可用 .contents 改写字段。


def _patch_thread_chain(monkeypatch, gui_thread_info_ret: int, write_info=None) -> None:
    """patch caret 模块的 GetForegroundWindow/GetWindowThreadProcessId/GetGUIThreadInfo。

    write_info(structure): 可选，接收已 cast 出来的 _GUITHREADINFO 实例，
    直接设置其命名属性（hwndFocus / flags / rcCaret.*）。
    """
    from dictashift.output import caret

    monkeypatch.setattr(caret._user32, "GetForegroundWindow", lambda: 12345)
    monkeypatch.setattr(
        caret._user32, "GetWindowThreadProcessId", lambda hwnd, pid_ptr: 6789
    )

    def _ggi(thread_id, info_ptr):
        if write_info is not None:
            # info_ptr 是 ctypes.byref 产生的 CArgObject，无 .contents；
            # cast 成正确的 POINTER 后访问 .contents 写入字段。
            casted = ctypes.cast(info_ptr, ctypes.POINTER(caret._GUITHREADINFO))
            write_info(casted.contents)
        return gui_thread_info_ret

    monkeypatch.setattr(caret._user32, "GetGUIThreadInfo", _ggi)


def test_has_focused_caret_relaxed_when_hwndfocus_set_no_caret_flags(monkeypatch) -> None:
    """hwndFocus 非空但无 caret 标志（自绘框架场景）-> True（宽松放行）。

    对应 caret.py 末尾分支：hwndFocus 非空 + flags 无 GUI_CARETBLINKING +
    rcCaret 全零 -> 仍乐观返回 True（Qt/Electron 等自绘 caret 框架照常接受输入）。
    """
    from dictashift.output import caret

    def _write(info) -> None:
        # hwndFocus 非空、flags=0（无 caret 标志）、rcCaret 全零
        info.hwndFocus = 9999
        info.flags = 0
        info.rcCaret.left = 0
        info.rcCaret.top = 0
        info.rcCaret.right = 0
        info.rcCaret.bottom = 0

    _patch_thread_chain(monkeypatch, gui_thread_info_ret=1, write_info=_write)
    assert caret.has_focused_caret() is True


def test_has_focused_caret_false_when_hwndfocus_empty(monkeypatch) -> None:
    """hwndFocus 为空 -> False。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 0  # 空
        info.flags = 1

    _patch_thread_chain(monkeypatch, gui_thread_info_ret=1, write_info=_write)
    assert caret.has_focused_caret() is False


def test_has_focused_caret_false_when_api_fails(monkeypatch) -> None:
    """GetGUIThreadInfo 返回 0（API 失败）-> False。"""
    from dictashift.output import caret

    _patch_thread_chain(monkeypatch, gui_thread_info_ret=0, write_info=None)
    assert caret.has_focused_caret() is False


def test_has_focused_caret_true_when_caret_blinking_flag(monkeypatch) -> None:
    """flags 含 GUI_CARETBLINKING -> True（标准编辑控件场景）。"""
    from dictashift.output import caret

    def _write(info) -> None:
        info.hwndFocus = 9999
        info.flags = caret._GUI_CARETBLINKING  # 0x1

    _patch_thread_chain(monkeypatch, gui_thread_info_ret=1, write_info=_write)
    assert caret.has_focused_caret() is True


def test_has_focused_caret_false_when_no_foreground_window(monkeypatch) -> None:
    """GetForegroundWindow 返回 0（无前台窗口）-> False。"""
    from dictashift.output import caret

    monkeypatch.setattr(caret._user32, "GetForegroundWindow", lambda: 0)
    assert caret.has_focused_caret() is False
