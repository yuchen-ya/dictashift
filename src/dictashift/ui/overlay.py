"""状态悬浮窗：一张深色圆角半透明卡片，单行展示状态或结果文本。

结构：
- 根 QWidget 透明（WA_TranslucentBackground），根 layout margins=0 只装一个
  ``card``（objectName=overlayCard）容器；card 用 stylesheet 画
  ``rgba(30,30,30,200)`` 圆角（12px）背景。
- card 内部 QVBoxLayout（左右 14、上下 8）只有**一行**：QHBoxLayout，
  包含一个 10x10 圆点 QLabel（随状态动态改 ``background-color`` +
  ``border-radius:5px``）+ 一个文字 QLabel ``_text_label``（#f0f0f0、9pt、
  ``setWordWrap(True)``、``setMaximumWidth(360)``）+ stretch。

行为 (任务规格)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool）。
- 卡片里始终只有一处文字（``_text_label``），按优先级显示：
  1. 反馈期间（点击复制后）显示「已复制 ✓」/「复制失败」，1.5s 后恢复；
  2. ``_override_text`` 非空时显示它（结果或错误文本，show_result/show_error 设置）；
  3. 否则显示 ``_STATE_TEXT`` 状态文案。
- set_state(state)：更新圆点颜色与状态文案缓存；进入新任务状态
  （PREPARING/RECORDING/TRANSCRIBING/TRANSLATING）时清除 ``_override_text``
  （新任务开始隐藏上次结果）；然后刷新文本。
- 默认贴屏幕右上角；可用鼠标拖动改变位置。
- 点击卡片（mousePress + mouseRelease 同位置，移动 < 6px 视为点击）：
  若当前显示的是 override 文本（结果/错误），则复制该文本，成功显示「已复制 ✓」、
  失败显示「复制失败」，1.5s 后恢复原 override 文本；若仅显示状态文案（无 override），
  点击不做任何事（只有拖动）。``_override_text`` 非空时光标变为 PointingHandCursor。
- show_result(text, failed=True)：设 ``_override_text = text`` 并刷新（failed 参数
  保留以兼容 application 调用，但显示内容就是 text 本身，不再区分前缀）。
- show_error(message)：设 ``_override_text = message``。
- 宽度由内容决定（adjustSize 自然驱动，不 setFixedWidth；移除了 setMinimumWidth
  以避免与 adjustSize 冲突产生 setGeometry 最小尺寸警告）。长文本宽到 ~400px 自然换行。

result_ui=="overlay" 模式下，本窗口长期可见；result_ui=="popup" 模式下，
本窗口由 application 层隐藏（不在这里弹 ResultPopup）。

线程模型：纯 UI 主线程；状态更新由 application 层通过 queued signal 触发。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..core.state import AppState
from ..output.clipboard import OutputError, set_clipboard_text
from .theme import ACCENT, CARD_BG, CARD_TEXT


# ==================== 状态文案与颜色映射 ====================

_STATE_TEXT: dict[AppState, str] = {
    AppState.IDLE: "就绪",
    AppState.PREPARING: "◌ 准备中…",
    AppState.RECORDING: "● 录音中…",
    AppState.TRANSCRIBING: "识别中…",
    AppState.TRANSLATING: "翻译中…",
    AppState.REWRITING: "改写中…",
    AppState.INSERTING: "粘贴中…",
    AppState.DISABLED: "已禁用",
    AppState.ERROR: "出错了",
}

_STATE_COLOR: dict[AppState, str] = {
    AppState.IDLE: "#9ca3af",          # 灰
    AppState.PREPARING: "#f59e0b",     # 橙，麦克风打开中
    AppState.RECORDING: "#ef4444",     # 红，醒目
    AppState.TRANSCRIBING: "#3b82f6",  # 蓝
    AppState.TRANSLATING: "#8b5cf6",   # 紫
    AppState.REWRITING: ACCENT,        # 主题 accent 紫
    AppState.INSERTING: "#10b981",     # 绿
    AppState.DISABLED: "#6b7280",      # 暗灰
    AppState.ERROR: "#ef4444",         # 红
}

# 状态变为这些（新任务开始）时清除 override（隐藏上次结果）。
_TASK_START_STATES = frozenset(
    {AppState.PREPARING, AppState.RECORDING, AppState.TRANSCRIBING, AppState.TRANSLATING}
)

# 「已复制」/「复制失败」反馈保持时间（毫秒），到期恢复原文本。
_FEEDBACK_MS = 1_500
# 文本 Label 最大宽度（像素）：长文本在此宽度内自动换行，避免卡片过宽。
_TEXT_MAX_WIDTH = 360
# 圆点尺寸（像素）。
_DOT_SIZE = 10
# 点击判定阈值（像素）：mousePress 到 mouseRelease 移动距离 < 此值视为点击。
_CLICK_THRESHOLD_PX = 6

# 复制成功 / 失败反馈文案。
_COPIED_OK_TEXT = "已复制 ✓"
_COPIED_FAIL_TEXT = "复制失败"


class StateOverlay(QWidget):
    """状态悬浮窗：一张深色圆角半透明卡片，单行展示状态或结果文本。

    不变量：
    - 默认贴屏幕右上角；首次显示时定位。
    - 鼠标按下并拖动可移动；按下位置作为偏移基准。
    - set_visible_enabled(False) 时隐藏，状态变化不强行显示。
    - 卡片文字按 反馈 > override > 状态文案 的优先级显示；点击 override 文本即复制。
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
        # override 文本（show_result/show_error 设置）；为 None 表示仅显示状态文案。
        self._override_text: str | None = None

        self.setWindowTitle("DictaShift")
        # 根窗口透明：圆角深色背景由内部 card 容器（#overlayCard）的 stylesheet 绘制。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 不设 setMinimumWidth：避免与 adjustSize 冲突触发
        # 「QWindowsWindow::setGeometry ... mintrack=...」最小尺寸警告。

        self._drag_offset: QPoint | None = None
        # 鼠标按下时的全局位置，用于 mouseRelease 时判定是否为点击。
        self._press_global_pos: QPoint | None = None

        # 根 layout margins=0，只装一个 card 容器。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 卡片容器：统一深色圆角半透明背景 + 文字美化（配色常量见 ui/theme.py）。
        card = QWidget(self)
        card.setObjectName("overlayCard")
        card.setStyleSheet(
            f"#overlayCard {{ background-color: {CARD_BG}; border-radius: 12px; }}"
            f" QLabel {{ color: {CARD_TEXT}; background: transparent; }}"
        )
        root.addWidget(card)
        self._card = card

        # card 内部布局：上下 8、左右 14、行间 0（只有一行）。
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 8, 14, 8)
        card_layout.setSpacing(0)

        # ---- 单行：圆点 + 文字 ----
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._dot_label = QLabel(card)
        self._dot_label.setFixedSize(_DOT_SIZE, _DOT_SIZE)
        # 圆点初始颜色（IDLE）；set_state 会动态更新。
        self._dot_label.setStyleSheet("background-color: #9ca3af; border-radius: 5px;")
        row.addWidget(self._dot_label)

        self._text_label = QLabel(_STATE_TEXT[AppState.IDLE], card)
        text_font = self._text_label.font()
        text_font.setPointSize(9)
        self._text_label.setFont(text_font)
        self._text_label.setWordWrap(True)
        self._text_label.setMaximumWidth(_TEXT_MAX_WIDTH)
        row.addWidget(self._text_label, stretch=1)
        row.addStretch(0)
        card_layout.addLayout(row)

        # 反馈计时：点击复制成功/失败后短暂显示反馈文案，到期恢复原文本。
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(_FEEDBACK_MS)
        self._feedback_timer.timeout.connect(self._on_feedback_timeout)

        self._refresh_text()

    # ==================== 公共接口 ====================

    def set_state(self, state: AppState) -> None:
        """更新圆点颜色；新任务开始时清除 override（隐藏上次结果）；刷新文本。"""
        new_task_started = state in _TASK_START_STATES
        self._state = state
        color = _STATE_COLOR.get(state, "#9ca3af")
        self._dot_label.setStyleSheet(
            f"background-color: {color}; border-radius: 5px;"
        )
        if new_task_started:
            self._override_text = None
            # 若反馈进行中也一并停掉，恢复状态文案。
            if self._feedback_timer.isActive():
                self._feedback_timer.stop()
        self._refresh_text()
        self._adjust_size()
        self.update()

    def set_visible_enabled(self, visible: bool) -> None:
        """由设置控制是否显示悬浮窗。"""
        self._enabled_visible = bool(visible)
        # 仅在允许显示时让窗口可见；不允许时隐藏。
        self.setVisible(self._enabled_visible)

    def show_result(self, text: str, failed: bool = True) -> None:
        """显示结果文本（直接替换状态文字位置）。

        failed 参数保留以兼容 application 调用，但显示内容就是 text 本身
        （不再区分前缀，也不再展示复制按钮——点击卡片任意处即复制）。
        """
        del failed  # 兼容形参，不再使用。
        self._override_text = text
        self._text_label.setToolTip(text)
        # 若正处于反馈期，先停掉以立即显示 override 文本。
        if self._feedback_timer.isActive():
            self._feedback_timer.stop()
        self._refresh_text()
        self._adjust_size()

    def show_error(self, message: str) -> None:
        """以错误文本形式展示消息：与 show_result 同行为（显示 message 本身）。"""
        self._override_text = message
        self._text_label.setToolTip(message)
        if self._feedback_timer.isActive():
            self._feedback_timer.stop()
        self._refresh_text()
        self._adjust_size()

    # ==================== 内部：文本刷新 ====================

    def _current_text(self) -> str:
        """返回当前应显示的文本（不含反馈期临时文案；反馈文案由 mouseRelease 临时设置）。"""
        if self._override_text is not None:
            return self._override_text
        return _STATE_TEXT.get(self._state, "就绪")

    def _refresh_text(self) -> None:
        """按 override > 状态文案 的优先级刷新 _text_label；并更新光标提示。"""
        self._text_label.setText(self._current_text())
        # override 非空 -> 可点击复制 -> 手型光标；否则默认箭头。
        if self._override_text is not None:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()
        # 文本变化后按内容收窄/封顶文本 label 宽度。
        self._apply_text_width()

    def _on_feedback_timeout(self) -> None:
        """反馈期结束：恢复显示 override 文本（或状态文案）。"""
        self._refresh_text()

    def _apply_text_width(self) -> None:
        """按当前 _text_label 文本计算内容宽度并 setFixedWidth。

        wordWrap QLabel 的 sizeHint 不收窄（这是历史卡片"就绪"两字也占 340px
        的根因）。这里逐行取 horizontalAdvance 最大值 natural，内容宽取
        min(natural, _TEXT_MAX_WIDTH)，setFixedWidth 让 label 真正按内容
        收缩；短文本（如「就绪」）立刻收窄到几十像素，长文本封顶
        _TEXT_MAX_WIDTH 并由 wordWrap 换行。

        注意：仅 setFixedWidth(label) + adjustSize() 在 Qt/Windows 上无法把
        窗口收窄到默认最小宽度（~200px）以下，因此同时按卡片整体宽度
        setFixedWidth(窗口)，确保短文本真正变窄。
        """
        fm = self._text_label.fontMetrics()
        text = self._text_label.text()
        lines = text.splitlines() or [""]
        natural = max(fm.horizontalAdvance(line) for line in lines)
        # horizontalAdvance 与 QLabel(QTextLayout) 换行判定存在数像素偏差：
        # 实测宽度恰好等于 advance 时仍会误换行，加少量余量避免短文本折行。
        content_w = min(natural + 6, _TEXT_MAX_WIDTH)
        self._text_label.setFixedWidth(content_w)
        # 卡片整体宽 = 文本内容宽 + 圆点 + 间距 + 卡片左右内边距(14*2)。
        card_w = content_w + _DOT_SIZE + 8 + 14 * 2
        self.setFixedWidth(card_w)

    def _adjust_size(self) -> None:
        """根据内容更新窗口高度（宽度已由 _apply_text_width 锁定）。

        高度由 wordWrap 换行自然决定，adjustSize 让窗口随换行行数增长。
        """
        self.adjustSize()

    # ==================== 拖动 + 点击复制 ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 记录按下时全局位置与窗口左上角的偏移（用于拖动）。
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            # 记录按下全局位置（用于 mouseRelease 判定是否为点击）。
            self._press_global_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._press_global_pos is not None:
            # 移动距离 < 阈值 -> 视为点击：若当前有 override 文本则复制。
            moved = (event.globalPosition().toPoint() - self._press_global_pos).manhattanLength()
            if moved < _CLICK_THRESHOLD_PX and self._override_text is not None:
                self._do_copy()
        self._drag_offset = None
        self._press_global_pos = None
        event.accept()

    def _do_copy(self) -> None:
        """复制当前 override 文本到剪贴板，成功/失败短暂显示反馈文案。"""
        text = self._override_text or ""
        try:
            set_clipboard_text(text)
            self._text_label.setText(_COPIED_OK_TEXT)
        except OutputError:
            self._text_label.setText(_COPIED_FAIL_TEXT)
        self._feedback_timer.start()

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
