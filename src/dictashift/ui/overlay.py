"""状态悬浮窗：无边框置顶半透明小条，显示当前状态文字与颜色点。

行为 (任务规格)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool）。
- 半透明圆角小条，显示状态文字 + 颜色点。
- set_state(state)：映射显示文案（见 _STATE_TEXT / _STATE_COLOR）。
- 默认贴屏幕右上角；可用鼠标拖动改变位置。
- set_visible_enabled(bool)：由设置控制是否显示。

线程模型：纯 UI 主线程；状态更新由 application 层通过 queued signal 触发。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QBrush,
    QPaintEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import QWidget

from ..core.state import AppState


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


class StateOverlay(QWidget):
    """状态悬浮窗。

    不变量：
    - 默认贴屏幕右上角；首次显示时定位。
    - 鼠标按下并拖动可移动；按下位置作为偏移基准。
    - set_visible_enabled(False) 时隐藏，状态变化不强行显示。
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

        self.setFixedHeight(34)
        self.setMinimumWidth(120)
        self.setWindowTitle("DictaShift")
        # 半透明背景；圆角通过 paintEvent 绘制。
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._drag_offset: QPoint | None = None
        # 初始宽度度量
        self._adjust_width()

    # ==================== 公共接口 ====================

    def set_state(self, state: AppState) -> None:
        """更新状态文案与颜色点；同步窗口宽度以容纳文案。"""
        self._state = state
        self._adjust_width()
        self.update()

    def set_visible_enabled(self, visible: bool) -> None:
        """由设置控制是否显示悬浮窗。"""
        self._enabled_visible = bool(visible)
        # 仅在允许显示时让窗口可见；不允许时隐藏。
        self.setVisible(self._enabled_visible)

    # ==================== 内部工具 ====================

    def _adjust_width(self) -> None:
        """根据当前状态文案度量并设置窗口宽度。

        在 paint 外预先度量，避免在 paintEvent 中 setFixedWidth 引发递归绘制。
        """
        text = _STATE_TEXT.get(self._state, "就绪")
        font = QFont()
        font.setPointSize(9)
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        # 左 padding(14) + dot(10) + gap(8) + text + right padding(14)
        desired_width = 14 + 10 + 8 + text_width + 14
        if self.width() != desired_width:
            self.setFixedWidth(desired_width)

    # ==================== 绘制 ====================

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

        # 颜色点：左侧。drawEllipse 参数为 (x, y, width, height)。
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
