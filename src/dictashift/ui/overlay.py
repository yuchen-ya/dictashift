"""状态悬浮窗：无边框置顶半透明小条，显示当前状态文字与颜色点。

行为 (任务规格)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool）。
- 半透明圆角小条，显示状态文字 + 颜色点。
- set_state(state)：映射显示文案（见 _STATE_TEXT / _STATE_COLOR）。
  新任务开始（recording/transcribing/translating）时隐藏结果区。
- 默认贴屏幕右上角；可用鼠标拖动改变位置。
- set_visible_enabled(bool)：由设置控制是否显示。
- show_result(text)：常驻悬浮窗模式下，显示结果区（前缀+文本+复制按钮）；
  复制按钮点击调 set_clipboard_text，成功后按钮短暂回显「已复制 ✓」。

result_ui=="overlay" 模式下，本窗口长期可见；result_ui=="popup" 模式下，
本窗口由 application 层隐藏（不在这里弹 ResultPopup）。

线程模型：纯 UI 主线程；状态更新由 application 层通过 queued signal 触发。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QBrush,
    QPaintEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.state import AppState
from ..output.clipboard import OutputError, set_clipboard_text


# ==================== 状态文案与颜色映射 ====================

_STATE_TEXT: dict[AppState, str] = {
    AppState.IDLE: "就绪",
    AppState.RECORDING: "● 录音中…",
    AppState.TRANSCRIBING: "识别中…",
    AppState.TRANSLATING: "翻译中…",
    AppState.INSERTING: "粘贴中…",
    AppState.DISABLED: "已禁用",
    AppState.ERROR: "出错了",
}

_STATE_COLOR: dict[AppState, str] = {
    AppState.IDLE: "#9ca3af",          # 灰
    AppState.RECORDING: "#ef4444",     # 红，醒目
    AppState.TRANSCRIBING: "#3b82f6",  # 蓝
    AppState.TRANSLATING: "#8b5cf6",   # 紫
    AppState.INSERTING: "#10b981",     # 绿
    AppState.DISABLED: "#6b7280",      # 暗灰
    AppState.ERROR: "#ef4444",         # 红
}

# 状态变为这些（新任务开始）时隐藏结果区。
_TASK_START_STATES = frozenset(
    {AppState.RECORDING, AppState.TRANSCRIBING, AppState.TRANSLATING}
)

# 「已复制」回显保持时间（毫秒）。
_COPIED_FEEDBACK_MS = 1_500
# 结果区文本最大宽度（像素），避免长文本把悬浮窗撑得过宽。
_RESULT_TEXT_MAX_WIDTH = 280


class _StateRow(QWidget):
    """状态行：自定义绘制圆角半透明条 + 颜色点 + 状态文案。

    仅绘制单行；度量由父窗口在 set_state 时驱动。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: AppState = AppState.IDLE
        self.setFixedHeight(34)
        self._adjust_width()

    def set_state(self, state: AppState) -> None:
        self._state = state
        self._adjust_width()
        self.update()

    def _adjust_width(self) -> None:
        text = _STATE_TEXT.get(self._state, "就绪")
        font = QFont()
        font.setPointSize(9)
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        # 左 padding(14) + dot(10) + gap(8) + text + right padding(14)
        desired_width = 14 + 10 + 8 + text_width + 14
        self.setFixedWidth(desired_width)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect()
        # 半透明圆角背景
        painter.setBrush(QBrush(QColor(30, 30, 30, 200)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(rect, 14, 14)

        text = _STATE_TEXT.get(self._state, "就绪")
        color = QColor(_STATE_COLOR.get(self._state, "#9ca3af"))

        # 颜色点：左侧。
        dot_diameter = 10
        dot_y = rect.center().y() - dot_diameter // 2
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(14, dot_y, dot_diameter, dot_diameter)

        # 文本：颜色点右侧
        painter.setPen(QColor(240, 240, 240))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        text_x = 14 + dot_diameter + 8
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(text)
        painter.drawText(
            text_x,
            rect.top(),
            text_width,
            rect.height(),
            Qt.AlignVCenter | Qt.AlignLeft,
            text,
        )


class StateOverlay(QWidget):
    """状态悬浮窗。

    不变量：
    - 默认贴屏幕右上角；首次显示时定位。
    - 鼠标按下并拖动可移动；按下位置作为偏移基准。
    - set_visible_enabled(False) 时隐藏，状态变化不强行显示。
    - 结果区默认隐藏；show_result 显示；set_state 进入新任务状态时隐藏。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        super().__init__(parent, flags)
        self._state: AppState = AppState.IDLE
        self._enabled_visible: bool = True  # 设置允许显示
        self._positioned: bool = False

        self.setMinimumWidth(120)
        self.setWindowTitle("DictaShift")
        # 半透明背景；圆角通过子部件 paintEvent 绘制。
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._drag_offset: QPoint | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self._state_row = _StateRow(self)
        root.addWidget(self._state_row)

        # 结果区（默认隐藏）：前缀 + 文本 + 复制按钮。
        result_wrap = QWidget(self)
        result_row = QHBoxLayout(result_wrap)
        result_row.setContentsMargins(14, 8, 14, 10)
        result_row.setSpacing(8)
        result_row.addWidget(self._make_label("直输失败：", bold=True))
        self._result_text_label = QLabel("", result_wrap)
        self._result_text_label.setWordWrap(False)
        self._result_text_label.setMaximumWidth(_RESULT_TEXT_MAX_WIDTH)
        self._result_text_label.setStyleSheet("color: #f0f0f0;")
        result_row.addWidget(self._result_text_label, stretch=1)
        self._copy_btn = QPushButton("复制", result_wrap)
        self._copy_btn.clicked.connect(self._on_copy_clicked)
        result_row.addWidget(self._copy_btn)
        self._result_wrap = result_wrap
        self._result_wrap.hide()
        root.addWidget(self._result_wrap)

        # 「已复制」回显计时：到期恢复按钮文字。
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(_COPIED_FEEDBACK_MS)
        self._copied_timer.timeout.connect(self._reset_copy_button_text)

        self._adjust_size()

    def _make_label(self, text: str, bold: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #f0f0f0;")
        f = lbl.font()
        if bold:
            f.setBold(True)
        lbl.setFont(f)
        return lbl

    # ==================== 公共接口 ====================

    def set_state(self, state: AppState) -> None:
        """更新状态文案与颜色点；新任务开始时隐藏结果区。"""
        new_task_started = state in _TASK_START_STATES
        self._state = state
        self._state_row.set_state(state)
        if new_task_started:
            self._hide_result()
        self._adjust_size()
        self.update()

    def set_visible_enabled(self, visible: bool) -> None:
        """由设置控制是否显示悬浮窗。"""
        self._enabled_visible = bool(visible)
        # 仅在允许显示时让窗口可见；不允许时隐藏。
        self.setVisible(self._enabled_visible)

    def show_result(self, text: str) -> None:
        """显示结果区，更新文本与尺寸。"""
        self._result_text_label.setText(text)
        self._result_text_label.setToolTip(text)
        self._reset_copy_button_text()
        self._result_wrap.show()
        self._adjust_size()

    # ==================== 内部：结果区 ====================

    def _hide_result(self) -> None:
        self._result_wrap.hide()

    def _adjust_size(self) -> None:
        """根据子部件可见性更新窗口尺寸（高度随结果区显隐变化）。"""
        self.adjustSize()
        # 保证最小宽度（容纳状态行）。
        if self.width() < self._state_row.width():
            self.setFixedWidth(self._state_row.width())

    def _on_copy_clicked(self) -> None:
        """点击「复制」：写剪贴板，成功则按钮短暂回显「已复制 ✓」。"""
        text = self._result_text_label.text()
        try:
            set_clipboard_text(text)
        except OutputError:
            self._copy_btn.setText("复制失败")
            self._copied_timer.start()
        else:
            self._copy_btn.setText("已复制 ✓")
            self._copied_timer.start()

    def _reset_copy_button_text(self) -> None:
        self._copy_btn.setText("复制")

    # ==================== 拖动 ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 记录按下时全局位置与窗口左上角的偏移。
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        event.accept()

    # ==================== 显示时定位 ====================

    def showEvent(self, event) -> None:  # noqa: N802
        if not self._positioned:
            self._positioned = True
            # 默认贴屏幕右上角，留 20px 边距。
            screen = self.screen() if self.screen() is not None else None
            if screen is not None:
                geo = screen.availableGeometry()
                self.move(geo.topRight().x() - self.width() - 20, geo.top() + 20)
        super().showEvent(event)
