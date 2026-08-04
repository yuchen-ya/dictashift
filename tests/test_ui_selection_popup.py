"""SelectionPopup（划词翻译极简悬浮卡片）的 offscreen UI 测试。

测试目标：
- 两态切换：show_result（译文可复制）/ show_error（简短错误文本，可复制）。
  无 loading 态——弹窗只在有最终结果时出现。
- 极简自适应：短译文是一小条（宽度收缩、文本区一行高）；长译文宽度封顶
  _MAX_WIDTH 自动换行、文本区高度封顶 _MAX_TEXT_HEIGHT 内部滚动。
- 不抢焦点：WA_ShowWithoutActivating 属性设置；Frameless|StaysOnTop|Tool flags。
- 自动关闭：QTimer 单发、interval 由构造参数/set_auto_close_seconds 决定、
  timeout 触发 hide；用户交互（鼠标按下）重置计时。
- 重复触发：替换内容与状态、重置计时、按新光标位置重新定位。
- 定位：_place_near_cursor 夹取在屏幕 availableGeometry 内（防出屏）。
- 复制按钮写剪贴板；关闭×按钮隐藏弹窗。
"""

from __future__ import annotations

import os

# 确保在导入 QApplication 之前设置 offscreen 平台。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from dictashift.output.clipboard import get_clipboard_text
from dictashift.ui.selection_popup import (
    _CURSOR_OFFSET_X,
    _CURSOR_OFFSET_Y,
    _MAX_TEXT_HEIGHT,
    _MAX_WIDTH,
    _MIN_WIDTH,
    SelectionPopup,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ==================== 窗口属性（无边框/置顶/不抢焦点） ====================


class TestWindowAttributes:
    def test_frameless_stays_on_top_tool_flags(self, qapp) -> None:
        popup = SelectionPopup()
        flags = popup.windowFlags()
        assert flags & Qt.FramelessWindowHint
        assert flags & Qt.WindowStaysOnTopHint
        assert flags & Qt.Tool

    def test_show_without_activating(self, qapp) -> None:
        """WA_ShowWithoutActivating 设置：弹窗不抢当前输入焦点。"""
        popup = SelectionPopup()
        assert popup.testAttribute(Qt.WA_ShowWithoutActivating) is True
        assert popup.testAttribute(Qt.WA_TranslucentBackground) is True


# ==================== 两态切换（无 loading 态） ====================


class TestStates:
    def test_no_loading_api(self, qapp) -> None:
        """对外 API 收敛为 show_result/show_error：不存在 show_loading。"""
        popup = SelectionPopup()
        assert not hasattr(popup, "show_loading")

    def test_show_result(self, qapp) -> None:
        popup = SelectionPopup()
        popup.show_result("translated text")
        assert popup._state == "result"
        assert popup._text_edit.toPlainText() == "translated text"
        assert popup._copy_btn.isEnabled() is True
        assert popup.isVisible() is True

    def test_show_error(self, qapp) -> None:
        popup = SelectionPopup()
        popup.show_error("翻译失败：boom")
        assert popup._state == "error"
        assert popup._text_edit.toPlainText() == "翻译失败：boom"
        # 错误文本可复制。
        assert popup._copy_btn.isEnabled() is True

    def test_retrigger_replaces_content_and_state(self, qapp) -> None:
        """已显示时再次触发：替换内容、重置状态、计时器重新启动。"""
        popup = SelectionPopup()
        popup.show_result("first")
        assert popup._state == "result"
        popup.show_error("boom")
        assert popup._state == "error"
        assert popup._text_edit.toPlainText() == "boom"
        # 计时器仍在跑（被重置）。
        assert popup._auto_close_timer.isActive() is True
        popup.show_result("second")
        assert popup._state == "result"
        assert popup._text_edit.toPlainText() == "second"
        assert popup._copy_btn.isEnabled() is True


# ==================== 极简自适应尺寸 ====================


class TestCompactSizing:
    def test_short_text_is_compact_strip(self, qapp) -> None:
        """短译文：宽度收缩到下界附近，文本区只有一行高（无大面积空白）。"""
        popup = SelectionPopup()
        popup.show_result("你好")
        qapp.processEvents()
        assert _MIN_WIDTH <= popup.width() <= 200
        # 单行文本区高度远小于长译文上限。
        assert popup._text_edit.height() < 60

    def test_long_single_line_wraps_at_max_width(self, qapp) -> None:
        """超长单行：宽度封顶 _MAX_WIDTH 并自动换行，高度随换行增长但有上限。"""
        popup = SelectionPopup()
        popup.show_result("word " * 200)
        qapp.processEvents()
        assert popup.width() == _MAX_WIDTH
        assert popup._text_edit.height() <= _MAX_TEXT_HEIGHT

    def test_many_lines_capped_with_scroll(self, qapp) -> None:
        """多行长译文：文本区高度封顶 _MAX_TEXT_HEIGHT（超出内部紧凑滚动）。"""
        popup = SelectionPopup()
        popup.show_result("\n".join(f"第{i}行译文" for i in range(50)))
        qapp.processEvents()
        assert popup._text_edit.height() == _MAX_TEXT_HEIGHT


# ==================== 自动关闭计时 ====================


class TestAutoClose:
    def test_default_interval_10s(self, qapp) -> None:
        popup = SelectionPopup()
        assert popup._auto_close_timer.interval() == 10_000
        assert popup._auto_close_timer.isSingleShot() is True

    def test_constructor_auto_close_seconds(self, qapp) -> None:
        popup = SelectionPopup(auto_close_seconds=25)
        assert popup._auto_close_timer.interval() == 25_000

    def test_set_auto_close_seconds_updates_interval(self, qapp) -> None:
        popup = SelectionPopup()
        popup.set_auto_close_seconds(30)
        assert popup._auto_close_timer.interval() == 30_000

    def test_set_auto_close_seconds_restarts_active_timer(self, qapp) -> None:
        """计时进行中更新秒数：按新值重启（设置应用后即时生效）。"""
        popup = SelectionPopup(auto_close_seconds=60)
        popup.show_result("x")
        assert popup._auto_close_timer.isActive() is True
        popup.set_auto_close_seconds(5)
        assert popup._auto_close_timer.interval() == 5_000
        assert popup._auto_close_timer.isActive() is True

    def test_timeout_hides_popup(self, qapp) -> None:
        """自动关闭计时到期 -> 隐藏弹窗。"""
        popup = SelectionPopup()
        popup.show_result("will-hide")
        assert popup.isVisible() is True
        popup._auto_close_timer.timeout.emit()
        qapp.processEvents()
        assert popup.isVisible() is False

    def test_close_button_hides_popup(self, qapp) -> None:
        popup = SelectionPopup()
        popup.show_result("x")
        popup._close_btn.click()
        assert popup.isVisible() is False


# ==================== 复制按钮 ====================


class TestCopyButton:
    def test_copy_writes_clipboard(self, qapp) -> None:
        popup = SelectionPopup()
        popup.show_result("sel-copy-me")
        popup._copy_btn.click()
        assert get_clipboard_text() == "sel-copy-me"
        assert popup._copy_btn.text() == "已复制 ✓"

    def test_copy_failure_feedback(self, qapp, monkeypatch) -> None:
        """写剪贴板失败 -> 按钮回显「复制失败」，不关闭弹窗。"""
        from dictashift.output.clipboard import OutputError
        import dictashift.ui.selection_popup as popup_mod

        monkeypatch.setattr(
            popup_mod,
            "set_clipboard_text",
            lambda t: (_ for _ in ()).throw(OutputError("nope")),
        )
        popup = SelectionPopup()
        popup.show_result("will-fail")
        popup._copy_btn.click()
        assert popup._copy_btn.text() == "复制失败"
        assert popup.isVisible() is True

    def test_new_show_resets_copy_button_text(self, qapp) -> None:
        popup = SelectionPopup()
        popup.show_result("a")
        popup._copy_btn.click()
        assert popup._copy_btn.text() == "已复制 ✓"
        popup.show_result("b")
        assert popup._copy_btn.text() == "复制"


# ==================== 定位（光标右下方 + 边界夹取） ====================


class TestPlacement:
    def test_places_at_cursor_offset(self, qapp) -> None:
        """屏幕中央位置：窗口左上角 = 光标 + 右下偏移。"""
        popup = SelectionPopup()
        popup.show_result("hello")
        qapp.processEvents()
        screen = QGuiApplication.primaryScreen()
        assert screen is not None
        geo = screen.availableGeometry()
        center = geo.center()
        popup._place_near_cursor(center)
        assert popup.x() == center.x() + _CURSOR_OFFSET_X
        assert popup.y() == center.y() + _CURSOR_OFFSET_Y

    def test_clamped_at_bottom_right_corner(self, qapp) -> None:
        """光标在屏幕右下角：窗口被夹回屏幕内（防出屏）。"""
        popup = SelectionPopup()
        popup.show_result("corner text")
        qapp.processEvents()
        screen = QGuiApplication.primaryScreen()
        assert screen is not None
        geo = screen.availableGeometry()
        popup._place_near_cursor(QPoint(geo.right(), geo.bottom()))
        w = popup.width()
        h = popup.height()
        # 右/下边缘不越界；左/上边缘在屏幕内。
        assert popup.x() + w <= geo.right()
        assert popup.y() + h <= geo.bottom()
        assert popup.x() >= geo.left()
        assert popup.y() >= geo.top()

    def test_retrigger_repositions_to_new_cursor(self, qapp) -> None:
        """重复触发按新光标位置重新定位。"""
        popup = SelectionPopup()
        popup.show_result("pos1")
        qapp.processEvents()
        screen = QGuiApplication.primaryScreen()
        assert screen is not None
        geo = screen.availableGeometry()
        p1 = geo.topLeft() + QPoint(50, 50)
        popup._place_near_cursor(p1)
        pos1 = QPoint(popup.x(), popup.y())
        p2 = geo.topLeft() + QPoint(120, 90)
        # 模拟新一次触发：内容替换 + 重新定位。
        popup.show_result("pos2")
        popup._place_near_cursor(p2)
        assert (popup.x(), popup.y()) != (pos1.x(), pos1.y())
        assert popup.x() == p2.x() + _CURSOR_OFFSET_X
        assert popup.y() == p2.y() + _CURSOR_OFFSET_Y
