"""overlay 与 result_popup 的 offscreen UI 测试。

测试目标：
- StateOverlay.show_result：显示结果区、文本与尺寸更新。
- StateOverlay.set_state：进入 recording/transcribing/translating 时隐藏结果区。
- StateOverlay 复制按钮：点击写剪贴板（成功回显「已复制 ✓」）。
- ResultPopup._place_bottom_right：窗口定位到屏幕右下角 availableGeometry 右下 16px 内。
"""

from __future__ import annotations

import os

# 确保在导入 QApplication 之前设置 offscreen 平台。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from dictashift.core.state import AppState
from dictashift.output.clipboard import get_clipboard_text
from dictashift.ui.overlay import StateOverlay
from dictashift.ui.result_popup import ResultPopup


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ==================== StateOverlay ====================


class TestOverlayShowResult:
    def test_result_area_hidden_by_default(self, qapp) -> None:
        ov = StateOverlay()
        # 顶层窗口未 show 时 isVisible() 恒为 False；用 isHidden() 判断显式 hide。
        assert ov._result_wrap.isHidden()

    def test_show_result_displays_area(self, qapp) -> None:
        ov = StateOverlay()
        ov.show_result("hello world")
        assert not ov._result_wrap.isHidden()
        assert ov._result_text_label.text() == "hello world"
        assert ov._result_text_label.toolTip() == "hello world"

    def test_copy_button_writes_clipboard(self, qapp) -> None:
        ov = StateOverlay()
        ov.show_result("copy-me-xyz")
        ov._copy_btn.click()
        assert get_clipboard_text() == "copy-me-xyz"
        assert ov._copy_btn.text() == "已复制 ✓"


class TestOverlaySetStateHidesResult:
    @pytest.mark.parametrize(
        "state",
        [AppState.RECORDING, AppState.TRANSCRIBING, AppState.TRANSLATING],
    )
    def test_task_start_hides_result(self, qapp, state) -> None:
        ov = StateOverlay()
        ov.show_result("pending")
        assert not ov._result_wrap.isHidden()
        ov.set_state(state)
        assert ov._result_wrap.isHidden()

    def test_non_task_state_keeps_result(self, qapp) -> None:
        ov = StateOverlay()
        ov.show_result("pending")
        # IDLE / INSERTING / ERROR / DISABLED 不应隐藏结果区
        ov.set_state(AppState.IDLE)
        assert not ov._result_wrap.isHidden()
        ov.set_state(AppState.ERROR)
        assert not ov._result_wrap.isHidden()


# ==================== ResultPopup 定位 ====================


class TestResultPopupBottomRight:
    def test_place_bottom_right_within_margin(self, qapp) -> None:
        popup = ResultPopup()
        popup.show_result("some failed text")
        # 强制同步布局以获得真实尺寸。
        qapp.processEvents()

        screen = QGuiApplication.primaryScreen()
        assert screen is not None  # offscreen 仍提供 primaryScreen
        geo = screen.availableGeometry()

        w = popup.width()
        h = popup.height()

        # 窗口右下角应在 geo 右下角 16px 内（即窗口右/下边 >= geo.right/bottom - 16）。
        right_edge = popup.x() + w
        bottom_edge = popup.y() + h
        assert right_edge >= geo.right() - 16
        assert bottom_edge >= geo.bottom() - 16
        # 不超出可用区。
        assert popup.x() >= geo.left()
        assert popup.y() >= geo.top()

    def test_no_legacy_place_near_cursor(self, qapp) -> None:
        popup = ResultPopup()
        assert not hasattr(popup, "_place_near_cursor")
        assert hasattr(popup, "_place_bottom_right")


# ==================== 计时器辅助：等待 copied 回显恢复 ====================
# （_COPIED_FEEDBACK_MS=1500ms，offscreen 下直接等待稍慢；保留为可选冒烟。）


def test_overlay_copied_feedback_resets(qapp) -> None:
    ov = StateOverlay()
    ov.show_result("reset-me")
    ov._copy_btn.click()
    assert ov._copy_btn.text() == "已复制 ✓"
    # 手动触发计时到期（不等真实时钟）。
    if ov._copied_timer.isActive():
        # 用定时等待确认到期回调恢复按钮文字（1500ms）。
        timer = QElapsedTimer()
        timer.start()
        while ov._copied_timer.isActive() and not timer.hasExpired(4000):
            qapp.processEvents()
    assert ov._copy_btn.text() == "复制"
