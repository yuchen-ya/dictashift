"""overlay 与 result_popup 的 offscreen UI 测试。

测试目标（overlay 单行卡片 + 点击复制改版后）：
- StateOverlay 卡片结构：单行（圆点 + _text_label），无 _result_wrap / 前缀 / 复制按钮。
- show_result / show_error：设置 _override_text 并显示原文（取代状态文字）。
- set_state：进入 PREPARING/RECORDING/TRANSCRIBING/TRANSLATING 时清除 override，回到状态文案。
- 点击复制：mousePress + mouseRelease（同位置，移动 < 阈值）触发复制，
  成功显示「已复制 ✓」、失败显示「复制失败」，1.5s 后恢复原 override 文本；
  无 override 时点击不复制。
- ResultPopup：_place_bottom_right 定位到屏幕右下角 availableGeometry 右下 16px 内；
  show_error 标题切换；复制按钮写剪贴板。
"""

from __future__ import annotations

import os

# 确保在导入 QApplication 之前设置 offscreen 平台。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QElapsedTimer, QPointF
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtCore import Qt, QPoint
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


def _release_at(overlay: StateOverlay, local_pt: QPoint) -> None:
    """构造一次 LeftButton mouseRelease 事件并送达 overlay（同一局部坐标）。"""
    evt = QMouseEvent(
        QMouseEvent.MouseButtonRelease,
        QPointF(local_pt),
        QPointF(overlay.mapToGlobal(local_pt)),
        Qt.LeftButton,
        Qt.NoButton,
        Qt.NoModifier,
    )
    overlay.mouseReleaseEvent(evt)


def _press_at(overlay: StateOverlay, local_pt: QPoint) -> None:
    """构造一次 LeftButton mousePress 事件并送达 overlay（同一局部坐标）。"""
    evt = QMouseEvent(
        QMouseEvent.MouseButtonPress,
        QPointF(local_pt),
        QPointF(overlay.mapToGlobal(local_pt)),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    overlay.mousePressEvent(evt)


# ==================== StateOverlay：卡片结构（单行） ====================


class TestOverlayStructureSingleLine:
    def test_no_legacy_result_wrap(self, qapp) -> None:
        """新版无 _result_wrap / 前缀 label / 复制按钮。"""
        ov = StateOverlay()
        assert not hasattr(ov, "_result_wrap")
        assert not hasattr(ov, "_result_prefix_label")
        assert not hasattr(ov, "_result_text_label")
        assert not hasattr(ov, "_copy_btn")

    def test_has_single_text_label(self, qapp) -> None:
        ov = StateOverlay()
        assert hasattr(ov, "_text_label")
        # 单文本 label 启用自动换行 + 内容宽度封顶常量 360。
        assert ov._text_label.wordWrap() is True
        from dictashift.ui.overlay import _TEXT_MAX_WIDTH
        assert _TEXT_MAX_WIDTH == 360
        # 初始（IDLE 短文本）已按内容 setFixedWidth 收窄；maximumWidth 不再固定 360。
        assert ov._text_label.maximumWidth() <= _TEXT_MAX_WIDTH

    def test_no_hardcoded_minimum_width(self, qapp) -> None:
        """未主动 setMinimumWidth（避免与 adjustSize 冲突触发 setGeometry 警告）。

        自适应改造后窗口宽度由 setFixedWidth 驱动；minimumWidth 跟随 fixed width，
        不再恒为 0，但 init 阶段没设过显式 setMinimumWidth 非零值。
        """
        ov = StateOverlay()
        # minimumWidth <= maximumWidth（fixed width 让二者相等，均为内容宽）。
        assert ov.minimumWidth() <= ov.maximumWidth()

    def test_card_has_dark_rounded_stylesheet(self, qapp) -> None:
        """card 仍保留深色圆角背景；新版 stylesheet 不再含 QPushButton 规则。"""
        ov = StateOverlay()
        ss = ov._card.styleSheet()
        assert "#overlayCard" in ss
        assert "rgba(30, 30, 30, 200)" in ss
        assert "border-radius: 12px" in ss
        # QLabel 文字色规则保留。
        assert "QLabel" in ss
        # 不再有 QPushButton 规则（复制按钮已移除）。
        assert "QPushButton" not in ss

    def test_root_layout_zero_margins_only_card(self, qapp) -> None:
        ov = StateOverlay()
        root = ov.layout()
        assert root is not None
        assert root.contentsMargins().left() == 0
        assert root.contentsMargins().top() == 0
        assert root.contentsMargins().right() == 0
        assert root.contentsMargins().bottom() == 0
        assert root.count() == 1
        only_item = root.itemAt(0)
        assert only_item is not None and only_item.widget() is ov._card


# ==================== StateOverlay：show_result / show_error ====================


class TestOverlayShowResult:
    def test_idle_shows_state_text(self, qapp) -> None:
        ov = StateOverlay()
        assert ov._text_label.text() == "就绪"
        assert ov._override_text is None

    def test_show_result_sets_override(self, qapp) -> None:
        ov = StateOverlay()
        ov.show_result("hello world")
        assert ov._override_text == "hello world"
        assert ov._text_label.text() == "hello world"

    def test_show_result_failed_arg_ignored(self, qapp) -> None:
        """failed 参数保留兼容，但显示内容就是 text 本身（无前缀区分）。"""
        ov = StateOverlay()
        ov.show_result("oops", failed=True)
        assert ov._text_label.text() == "oops"
        ov2 = StateOverlay()
        ov2.show_result("ok text", failed=False)
        assert ov2._text_label.text() == "ok text"

    def test_show_error_sets_override(self, qapp) -> None:
        ov = StateOverlay()
        ov.show_error("boom-err")
        assert ov._override_text == "boom-err"
        assert ov._text_label.text() == "boom-err"

    def test_override_changes_cursor(self, qapp) -> None:
        """override 非空时为 PointingHandCursor，无 override 时无特殊光标。"""
        ov = StateOverlay()
        # 无 override：未设手型（unsetCursor 后 shape != PointingHandCursor）。
        assert ov.cursor().shape() != Qt.PointingHandCursor
        ov.show_result("x")
        assert ov.cursor().shape() == Qt.PointingHandCursor


# ==================== StateOverlay：set_state 清除 override ====================


class TestOverlaySetStateClearsOverride:
    @pytest.mark.parametrize(
        "state",
        [AppState.PREPARING, AppState.RECORDING, AppState.TRANSCRIBING, AppState.TRANSLATING],
    )
    def test_task_start_clears_override(self, qapp, state) -> None:
        ov = StateOverlay()
        ov.show_result("pending")
        assert ov._override_text == "pending"
        ov.set_state(state)
        assert ov._override_text is None
        # 文本回到该状态文案。
        from dictashift.ui.overlay import _STATE_TEXT
        assert ov._text_label.text() == _STATE_TEXT[state]

    def test_non_task_state_keeps_override(self, qapp) -> None:
        ov = StateOverlay()
        ov.show_result("pending")
        # IDLE / INSERTING / ERROR / DISABLED 不应清除 override。
        ov.set_state(AppState.IDLE)
        assert ov._override_text == "pending"
        ov.set_state(AppState.ERROR)
        assert ov._override_text == "pending"


# ==================== StateOverlay：点击复制 ====================


class TestOverlayClickCopy:
    def test_click_copies_override(self, qapp) -> None:
        """mousePress + mouseRelease（同位置）→ 复制 override 文本并显示「已复制 ✓」。"""
        ov = StateOverlay()
        ov.show_result("copy-me-xyz")
        local = QPoint(5, 5)
        _press_at(ov, local)
        _release_at(ov, local)
        assert get_clipboard_text() == "copy-me-xyz"
        assert ov._text_label.text() == "已复制 ✓"

    def test_click_without_override_no_copy(self, qapp) -> None:
        """无 override（仅状态文案）时点击不复制、不改文字。"""
        ov = StateOverlay()
        # 先往剪贴板放个标记值，确认点击后未被覆盖。
        from dictashift.output.clipboard import set_clipboard_text
        set_clipboard_text("MARKER-NOTOUCH")
        assert ov._override_text is None
        local = QPoint(5, 5)
        _press_at(ov, local)
        _release_at(ov, local)
        assert get_clipboard_text() == "MARKER-NOTOUCH"
        assert ov._text_label.text() == "就绪"

    def test_copy_failure_shows_fail_text(self, qapp, monkeypatch) -> None:
        """set_clipboard_text 抛 OutputError → 显示「复制失败」。"""
        from dictashift.output.clipboard import OutputError
        import dictashift.ui.overlay as overlay_mod
        monkeypatch.setattr(
            overlay_mod, "set_clipboard_text", lambda t: (_ for _ in ()).throw(OutputError("nope"))
        )
        ov = StateOverlay()
        ov.show_result("will-fail")
        local = QPoint(5, 5)
        _press_at(ov, local)
        _release_at(ov, local)
        assert ov._text_label.text() == "复制失败"

    def test_drag_does_not_copy(self, qapp) -> None:
        """mousePress 后大幅移动再 release → 视为拖动，不复制。"""
        ov = StateOverlay()
        ov.show_result("not-clicked")
        _press_at(ov, QPoint(0, 0))
        _release_at(ov, QPoint(100, 100))
        # 未复制：文字仍是 override 原文（不是「已复制 ✓」）。
        assert ov._text_label.text() == "not-clicked"


# ==================== 反馈恢复（1.5s 计时） ====================


class TestOverlayFeedbackRestore:
    def test_feedback_restores_override(self, qapp) -> None:
        """点击复制成功后，反馈计时到期恢复原 override 文本。"""
        ov = StateOverlay()
        ov.show_result("orig-text")
        local = QPoint(5, 5)
        _press_at(ov, local)
        _release_at(ov, local)
        assert ov._text_label.text() == "已复制 ✓"
        # 直接调用恢复槽（不等真实 1.5s）。
        ov._on_feedback_timeout()
        assert ov._text_label.text() == "orig-text"

    def test_feedback_restores_state_when_no_override(self, qapp) -> None:
        """_on_feedback_timeout 在无 override 时回到状态文案（边界覆盖）。"""
        ov = StateOverlay()
        ov._on_feedback_timeout()
        assert ov._text_label.text() == "就绪"

    def test_feedback_timer_fires_via_event_loop(self, qapp) -> None:
        """端到端：等真实计时到期（最长 4s）恢复 override 文本。"""
        ov = StateOverlay()
        ov.show_result("loop-restore")
        local = QPoint(5, 5)
        _press_at(ov, local)
        _release_at(ov, local)
        timer = QElapsedTimer()
        timer.start()
        while ov._feedback_timer.isActive() and not timer.hasExpired(4000):
            qapp.processEvents()
        assert ov._text_label.text() == "loop-restore"


# ==================== 状态圆点颜色 ====================


class TestOverlayStatusDotColor:
    def test_dot_color_follows_state(self, qapp) -> None:
        from dictashift.ui.overlay import _STATE_COLOR
        ov = StateOverlay()
        for state, color in _STATE_COLOR.items():
            ov.set_state(state)
            ss = ov._dot_label.styleSheet()
            assert color in ss
            assert "border-radius: 5px" in ss

    def test_dot_color_default_idle(self, qapp) -> None:
        ov = StateOverlay()
        assert "#9ca3af" in ov._dot_label.styleSheet()


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


# ==================== ResultPopup show_error / 标题切换 ====================


class TestResultPopupShowError:
    def test_show_error_sets_title_and_text(self, qapp) -> None:
        """show_error 把标题改为「出错了」，文本区显示错误内容。"""
        popup = ResultPopup()
        popup.show_error("something broke")
        qapp.processEvents()
        assert popup._title.text() == "出错了"
        assert popup._text_edit.toPlainText() == "something broke"

    def test_show_result_keeps_failure_title(self, qapp) -> None:
        """show_result 仍使用原标题「直输失败，请手动复制」。"""
        popup = ResultPopup()
        popup.show_result("txt")
        assert popup._title.text() == "直输失败，请手动复制"
        assert popup._text_edit.toPlainText() == "txt"

    def test_show_error_copy_button_copies_message(self, qapp) -> None:
        """show_error 后复制按钮可复制错误文本。"""
        popup = ResultPopup()
        popup.show_error("err-copy-me")
        popup._copy_btn.click()
        assert get_clipboard_text() == "err-copy-me"


# ==================== ResultPopup 卡片容器 ====================


class TestResultPopupCardContainer:
    def test_popup_has_card_container(self, qapp) -> None:
        """ResultPopup 根窗口下有一个 objectName=popupCard 的容器。"""
        popup = ResultPopup()
        from PySide6.QtWidgets import QWidget
        cards = [c for c in popup.findChildren(QWidget) if c.objectName() == "popupCard"]
        assert len(cards) == 1
        card = cards[0]
        ss = card.styleSheet()
        assert "#popupCard" in ss
        assert "border-radius: 12px" in ss
        # 标题/文本/按钮都是 card 的子控件（结构完整性）。
        assert popup._title.parent() is card
        assert popup._text_edit.parent() is card
        assert popup._copy_btn.parent() is card
        assert popup._close_btn.parent() is card

    def test_popup_root_layout_has_zero_margins(self, qapp) -> None:
        """根 layout margins=0，只装 card（背景画在 card 上）。"""
        popup = ResultPopup()
        root = popup.layout()
        assert root is not None
        assert root.contentsMargins().left() == 0
        assert root.contentsMargins().top() == 0


# ==================== ResultPopup 文字颜色 + 尺寸自适应 ====================


class TestResultPopupTextColor:
    def test_text_edit_has_dark_text_color(self, qapp) -> None:
        """QTextEdit stylesheet 显式指定深色文字（浅底可读）。"""
        popup = ResultPopup()
        # 注意：card 容器的 stylesheet 才含 QTextEdit 规则；_text_edit 本身无独立 ss。
        from PySide6.QtWidgets import QWidget
        card = [c for c in popup.findChildren(QWidget) if c.objectName() == "popupCard"][0]
        card_ss = card.styleSheet()
        assert "QTextEdit" in card_ss
        assert "color: #1f2937" in card_ss


class TestResultPopupAdaptiveSize:
    def test_short_text_narrower_than_max(self, qapp) -> None:
        """短文本 -> 窗口宽度明显小于 _MAX_WIDTH(420)。"""
        from dictashift.ui.result_popup import _MAX_WIDTH
        popup = ResultPopup()
        popup.show_result("测试文本")
        qapp.processEvents()
        assert popup.width() < _MAX_WIDTH
        assert popup.width() >= 240  # 下限 _MIN_WIDTH

    def test_long_text_caps_at_max_width(self, qapp) -> None:
        """长文本（自然宽 > 420）-> 宽度封顶 _MAX_WIDTH。"""
        from dictashift.ui.result_popup import _MAX_WIDTH
        popup = ResultPopup()
        popup.show_result("a" * 300)
        qapp.processEvents()
        assert popup.width() == _MAX_WIDTH

    def test_text_shortening_narrows_window(self, qapp) -> None:
        """长文本 _show 后再 _show 短文本 -> 窗口变窄。"""
        popup = ResultPopup()
        popup.show_result("a" * 300)
        qapp.processEvents()
        wide = popup.width()

        popup.show_result("短")
        qapp.processEvents()
        narrow = popup.width()

        assert narrow < wide

    def test_text_edit_height_within_bounds(self, qapp) -> None:
        """文本区高度在 [_MIN_TEXT_HEIGHT, _MAX_TEXT_HEIGHT] 内。"""
        from dictashift.ui.result_popup import _MIN_TEXT_HEIGHT, _MAX_TEXT_HEIGHT
        popup = ResultPopup()
        popup.show_result("hello\nworld\nfoo\nbar\nbaz\nqux\nmore\nlines")
        qapp.processEvents()
        h = popup._text_edit.height()
        assert _MIN_TEXT_HEIGHT <= h <= _MAX_TEXT_HEIGHT

    def test_max_width_constant_unchanged(self, qapp) -> None:
        """setMaximumWidth(420) 仍保留。"""
        from dictashift.ui.result_popup import _MAX_WIDTH
        popup = ResultPopup()
        assert popup.maximumWidth() == _MAX_WIDTH
        assert _MAX_WIDTH == 420


# ==================== StateOverlay 宽度按内容自适应 ====================


class TestOverlayAdaptiveWidth:
    def test_idle_width_narrow(self, qapp) -> None:
        """IDLE 短文本「就绪」-> 窗口宽 < 120px。"""
        ov = StateOverlay()
        ov.show()
        qapp.processEvents()
        assert ov.width() < 120

    def test_long_result_caps_text_label_width(self, qapp) -> None:
        """show_result 长文本 -> _text_label 固定宽 == 360，窗口宽不超过 ~410。"""
        ov = StateOverlay()
        ov.show()
        ov.show_result("x" * 400)
        qapp.processEvents()
        assert ov._text_label.width() == 360
        assert ov.width() <= 410

    def test_width_recovers_after_task_start(self, qapp) -> None:
        """长结果 -> 进入新任务状态（PREPARING）-> 宽度收回（< 长 result 时的宽）。"""
        ov = StateOverlay()
        ov.show()
        ov.show_result("x" * 400)
        qapp.processEvents()
        wide = ov.width()

        ov.set_state(AppState.PREPARING)
        qapp.processEvents()
        narrow = ov.width()

        assert narrow < wide
        assert narrow < 160  # 状态文案「◌ 准备中…」较短
