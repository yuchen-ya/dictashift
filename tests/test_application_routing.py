"""DictaShiftApplication 错误/结果路由单元测试。

测试目标：
- _on_error 按 settings.ui.result_ui 路由：overlay 模式调 overlay.show_error，
  否则调 result_popup.show_error；两种模式都保留 tray.show_message 与 overlay.set_state(ERROR)。
- _on_text_ready 按 result_ui 路由：
  - overlay 模式：直输成功（pasted=True）不打扰，直接 return（悬浮窗不显示结果）；
    直输失败（pasted=False）调 overlay.show_result(text, failed=True)。
  - popup 模式：pasted=True 直接 return；pasted=False 调 result_popup.show_result。

不构建完整 DictaShiftApplication（避免 ASR/recorder/hotkey/tray 等重依赖）。
用 __new__ 跳过 __init__，仅装配 _on_error/_on_text_ready 直接读取的属性：
self.settings.ui.result_ui、self._overlay、self._result_popup、self._tray。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dictashift.application import DictaShiftApplication
from dictashift.core.state import AppState
from dictashift.settings.models import AppSettings, UiSettings


class _FakeOverlay:
    """记录 show_result / show_error / set_state 调用的 overlay 替身。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []  # (text, failed)
        self.errors: list[str] = []
        self.states: list[AppState] = []

    def show_result(self, text: str, failed: bool = True) -> None:
        self.results.append((text, failed))

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def set_state(self, state: AppState) -> None:
        self.states.append(state)


class _FakePopup:
    """记录 show_result / show_error 调用的 ResultPopup 替身。"""

    def __init__(self) -> None:
        self.results: list[str] = []
        self.errors: list[str] = []

    def show_result(self, text: str) -> None:
        self.results.append(text)

    def show_error(self, message: str) -> None:
        self.errors.append(message)


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def show_message(self, title: str, body: str) -> None:
        self.messages.append((title, body))


def _make_app(result_ui: str) -> tuple[DictaShiftApplication, _FakeOverlay, _FakePopup, _FakeTray]:
    """跳过 __init__，仅装配路由方法直接依赖的属性。"""
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._overlay = _FakeOverlay()
    app._result_popup = _FakePopup()
    app._tray = _FakeTray()
    settings = AppSettings()
    settings.ui = UiSettings(result_ui=result_ui)
    app.settings = settings
    return app, app._overlay, app._result_popup, app._tray


# ==================== _on_error 路由 ====================


def test_on_error_popup_mode_routes_to_popup() -> None:
    """result_ui=popup 时 _on_error 调 result_popup.show_error（并保留 tray/overlay 状态）。"""
    app, overlay, popup, tray = _make_app("popup")
    app._on_error("boom")
    assert popup.errors == ["boom"]
    assert overlay.errors == []  # popup 模式不调 overlay.show_error
    # 仍保留 tray 通知与 overlay 状态置 ERROR
    assert tray.messages == [("DictaShift", "boom")]
    assert overlay.states == [AppState.ERROR]


def test_on_error_overlay_mode_routes_to_overlay() -> None:
    """result_ui=overlay 时 _on_error 调 overlay.show_error（并不弹 popup）。"""
    app, overlay, popup, tray = _make_app("overlay")
    app._on_error("err-x")
    assert overlay.errors == ["err-x"]
    assert popup.errors == []
    # tray 与 overlay 状态置 ERROR 仍保留
    assert tray.messages == [("DictaShift", "err-x")]
    assert overlay.states == [AppState.ERROR]


# ==================== _on_text_ready 路由 ====================


def test_on_text_ready_popup_success_does_not_disturb() -> None:
    """popup 模式 + pasted=True：直接 return，不打扰用户。"""
    app, overlay, popup, tray = _make_app("popup")
    app._on_text_ready("hello", True)
    assert popup.results == []
    assert overlay.results == []


def test_on_text_ready_popup_failure_shows_popup() -> None:
    """popup 模式 + pasted=False：弹 ResultPopup.show_result。"""
    app, overlay, popup, tray = _make_app("popup")
    app._on_text_ready("hello", False)
    assert popup.results == ["hello"]
    assert overlay.results == []


def test_on_text_ready_overlay_success_does_not_show() -> None:
    """overlay 模式 + pasted=True：直输成功，悬浮窗不显示结果（直接 return）。"""
    app, overlay, popup, tray = _make_app("overlay")
    app._on_text_ready("hello", True)
    assert overlay.results == []  # 不调 show_result
    assert popup.results == []


def test_on_text_ready_overlay_failure_shows_result() -> None:
    """overlay 模式 + pasted=False：show_result(failed=True) 在悬浮窗显示文本。"""
    app, overlay, popup, tray = _make_app("overlay")
    app._on_text_ready("hello", False)
    assert overlay.results == [("hello", True)]
    assert popup.results == []
