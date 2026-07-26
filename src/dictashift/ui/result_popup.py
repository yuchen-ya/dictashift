"""直输失败时的结果弹窗：显示转译全文 + 复制按钮。

行为 (任务规格 / AGENTS.md 5.5)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool），
  半透明圆角，与 overlay 风格一致。
- 内容：标题行 + 只读文本区（可选中、超长滚动、高度受限）+
  「复制」按钮（点击才写剪贴板）+ 「关闭」按钮。
- 弹窗显示本身不碰剪贴板；只有用户点击「复制」才调 set_clipboard_text。
- 位置：屏幕右下角，留 16px 边距（_place_bottom_right）。
- 15 秒无操作自动关闭（QTimer）；show_result 重复调用时更新文本、
  重新显示并重置自动关闭计时。

线程模型：纯 UI 主线程；由 application 层经 queued signal 调用 show_result。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QMouseEvent, QMoveEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..output.clipboard import OutputError, set_clipboard_text


# 自动关闭超时（毫秒）；无任何用户操作（点击复制/移动窗口）15 秒后自动关闭。
_AUTO_CLOSE_MS = 15_000
# 「已复制」回显保持时间（毫秒）。
_COPIED_FEEDBACK_MS = 1_500
# 弹窗最大尺寸（像素）：限制高度避免长文本撑满屏幕。
_MAX_WIDTH = 420
_MAX_HEIGHT = 240
# 自适应尺寸上下界（像素）：宽度随文本内容收缩，但不少于 _MIN_WIDTH；
# 文本区高度按行数增长但封顶 _MAX_TEXT_HEIGHT（超出内部滚动）。
_MIN_WIDTH = 240
_MIN_TEXT_HEIGHT = 60
_MAX_TEXT_HEIGHT = 160
# 文本区左右余量（卡片 padding + QTextEdit 自身边距近似）。
_TEXT_MARGIN_H = 24


class ResultPopup(QWidget):
    """直输失败时的结果弹窗：显示文本 + 复制按钮。

    不变量：
    - 显示本身不写剪贴板；只有用户点击「复制」才写。
    - 15 秒无操作自动关闭；任何用户交互（鼠标按下 / 复制点击）重置计时。
    - show_result 重复调用更新文本并重新显示、重置计时。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        super().__init__(parent, flags)
        self.setWindowTitle("DictaShift 结果")
        self.setMaximumWidth(_MAX_WIDTH)
        self.setMaximumHeight(_MAX_HEIGHT)
        # 根窗口透明：圆角深色背景由内部 card 容器（#popupCard）的 stylesheet 绘制，
        # 标题/按钮浮在卡片上而非直接画在桌面，与 overlay 结果行同风格。
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 根 layout margins=0，只装一个 card 容器。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 卡片容器：承载标题/文本区/按钮行，统一深色圆角半透明背景 + 按钮美化。
        card = QWidget(self)
        card.setObjectName("popupCard")
        card.setStyleSheet(
            "#popupCard { background-color: rgba(30, 30, 30, 215); border-radius: 12px; }"
            " QLabel { color: #f0f0f0; background: transparent; }"
            " QTextEdit { background-color: rgba(255, 255, 255, 235); color: #1f2937;"
            " border: none; border-radius: 8px; }"
            " QPushButton { background-color: rgba(255, 255, 255, 25); color: #f0f0f0;"
            " border: 1px solid rgba(255, 255, 255, 70); border-radius: 6px; padding: 3px 12px; }"
            " QPushButton:hover { background-color: rgba(255, 255, 255, 55); }"
            " QPushButton:pressed { background-color: rgba(255, 255, 255, 80); }"
        )
        root.addWidget(card)

        # 内容 layout 装在 card 上，间距/margins 保持原有数值。
        content = QVBoxLayout(card)
        content.setContentsMargins(12, 10, 12, 10)
        content.setSpacing(6)

        self._title = QLabel("直输失败，请手动复制", card)
        title_font = self._title.font()
        title_font.setBold(True)
        self._title.setFont(title_font)
        content.addWidget(self._title)

        self._text_edit = QTextEdit(card)
        self._text_edit.setReadOnly(True)
        self._text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 文本区高度在 _show 里按内容行数自适应（_MIN_TEXT_HEIGHT.._MAX_TEXT_HEIGHT）。
        content.addWidget(self._text_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._copy_btn = QPushButton("复制", card)
        self._copy_btn.clicked.connect(self._on_copy_clicked)
        self._close_btn = QPushButton("关闭", card)
        self._close_btn.clicked.connect(self.hide)
        btn_row.addWidget(self._copy_btn)
        btn_row.addWidget(self._close_btn)
        content.addLayout(btn_row)

        # 自动关闭计时：单次触发。
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.setInterval(_AUTO_CLOSE_MS)
        self._auto_close_timer.timeout.connect(self.hide)

        # 「已复制」回显计时：到期恢复按钮文字。
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(_COPIED_FEEDBACK_MS)
        self._copied_timer.timeout.connect(self._reset_copy_button_text)

    # ==================== 公共接口 ====================

    def show_result(self, text: str) -> None:
        """更新文本、重新定位（屏幕右下角）、显示并重置自动关闭计时。"""
        self._show(text, "直输失败，请手动复制")

    def show_error(self, message: str) -> None:
        """以错误形式展示消息：标题「出错了」，文本区显示 message。

        其余行为与 show_result 一致（右下角定位、15 秒自动关闭、复制按钮可复制错误文本）。
        """
        self._show(message, "出错了")

    def _show(self, text: str, title: str) -> None:
        """公共展示逻辑：更新标题与文本、按内容自适应尺寸、重新定位（屏幕右下角）、
        显示并重置计时。重复调用时若文本变短会变窄变矮。
        """
        self._title.setText(title)
        self._text_edit.setPlainText(text)
        self._reset_copy_button_text()
        self._adjust_size_to_content()
        self._place_bottom_right()
        self.show()
        self.raise_()
        self.activateWindow()
        self._auto_close_timer.start()

    # ==================== 内部：尺寸自适应 ====================

    def _adjust_size_to_content(self) -> None:
        """按当前文本内容重算窗口宽度与文本区高度。

        - 宽：取文本各行 horizontalAdvance 最大值 + 文本区左右余量，clamp 到
          [_MIN_WIDTH, _MAX_WIDTH]，setFixedWidth 到该值（保留 setMaximumWidth(420)）。
        - 文本区高：按行数（行高 × 行数 + 余量）clamp 到
          [_MIN_TEXT_HEIGHT, _MAX_TEXT_HEIGHT]，超出由 QTextEdit 内部滚动。
        - 卡片总高随之 adjustSize（保留 setMaximumHeight(240)）。
        """
        fm = self._text_edit.fontMetrics()
        text = self._text_edit.toPlainText()
        lines = text.splitlines() or [""]
        natural_text_w = max(fm.horizontalAdvance(line) for line in lines)
        desired_w = natural_text_w + _TEXT_MARGIN_H
        width = max(_MIN_WIDTH, min(desired_w, _MAX_WIDTH))
        self.setFixedWidth(width)

        line_height = fm.lineSpacing()
        text_h = line_height * len(lines) + 16  # 上下内边距近似
        text_h = max(_MIN_TEXT_HEIGHT, min(text_h, _MAX_TEXT_HEIGHT))
        self._text_edit.setFixedHeight(text_h)

        # 触发布局重算，让卡片高度跟随文本区收缩/增长。
        self.adjustSize()

    # ==================== 内部：复制 ====================

    def _on_copy_clicked(self) -> None:
        """用户点击「复制」：写剪贴板，成功则按钮短暂回显「已复制 ✓」。

        点击即重置自动关闭计时（用户活跃）。
        """
        text = self._text_edit.toPlainText()
        try:
            set_clipboard_text(text)
        except OutputError:
            # 写剪贴板失败：按钮回显失败提示，不关闭弹窗，用户可重试或手动选中复制。
            self._copy_btn.setText("复制失败")
            self._copied_timer.start()
        else:
            self._copy_btn.setText("已复制 ✓")
            self._copied_timer.start()
        self._auto_close_timer.start()

    def _reset_copy_button_text(self) -> None:
        self._copy_btn.setText("复制")

    # ==================== 内部：定位 ====================

    def _place_bottom_right(self) -> None:
        """把窗口定位到鼠标所在屏幕（否则主屏）的右下角，留 16px 边距。"""
        from PySide6.QtGui import QGuiApplication, QCursor

        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return  # 无屏幕可用（极端 offscreen），放弃定位。

        geo = screen.availableGeometry()

        # 评估尺寸：宽度已由 _adjust_size_to_content 锁定（setFixedWidth），
        # 直接用实际 width；高度用 sizeHint 上下界（实际由布局决定）。
        w = self.width() or self.sizeHint().width() or _MAX_WIDTH
        h = self.sizeHint().height() or _MAX_HEIGHT
        w = min(w, _MAX_WIDTH)
        h = min(h, _MAX_HEIGHT)

        # 右下角，留 16px 边距。窗口右下角对齐 geo.right()-16 / geo.bottom()-16。
        x = geo.right() - w - 16
        y = geo.bottom() - h - 16
        if x < geo.left():
            x = geo.left()
        if y < geo.top():
            y = geo.top()
        self.move(x, y)

    # ==================== 用户交互重置自动关闭计时 ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # 任意鼠标按下视为用户活跃，重置自动关闭。
        self._auto_close_timer.start()
        super().mousePressEvent(event)

    def moveEvent(self, event: QMoveEvent) -> None:  # noqa: N802
        # 窗口移动也视为活跃（例如被拖动）；初次定位的 move 也会触发，无副作用。
        if self._auto_close_timer.isActive():
            self._auto_close_timer.start()
        super().moveEvent(event)
