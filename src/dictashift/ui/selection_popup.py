"""划词翻译悬浮卡片：极简现代深色圆角卡片，直接显示译文。

行为 (任务规格)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool），
  复用 theme.py 统一深色圆角半透明悬浮卡片样式与 1px 细腻描边。
- 极简单卡：无标题栏、无独立文本编辑区底色；译文直接画在深色卡片上
  （QTextEdit 透明无边框，仅利用其换行/滚动/选中能力），留白舒适。
- 只在有最终结果时出现：对外 API 收敛为 show_result / show_error
  （无 loading 态——取词成功后弹窗不抢先出现，翻译完成才弹出）。
- 尺寸随内容自适应：宽度按文本自然宽度收缩（短译文就是一小条），
  超 _MAX_WIDTH 自动换行；文本高度按行数增长，超 _MAX_TEXT_HEIGHT 紧凑滚动。
- WA_ShowWithoutActivating + 不 activateWindow：显示时不抢焦点。
- 位置：显示时取 QCursor.pos() 偏移到光标右下方并夹取边界（防出屏）。
- QTimer 单发自动关闭；复制/鼠标按下重置计时。

线程模型：纯 UI 主线程；由 application 层经 queued signal 调用 show_* 方法。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..output.clipboard import OutputError, set_clipboard_text
from .icons import get_icon
from .theme import (
    CARD_BG_POPUP,
    SUCCESS,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    popup_card_stylesheet,
)


# 默认自动关闭秒数（构造参数缺省值；设置页可调 5~60）。
_DEFAULT_AUTO_CLOSE_S = 10
# 「已复制」回显保持时间（毫秒）。
_COPIED_FEEDBACK_MS = 1_500
# 弹窗最大宽度（像素）：超出自动换行。
_MAX_WIDTH = 420
# 文本区高度上限（像素）：长译文超出由文本区内部紧凑滚动。
_MAX_TEXT_HEIGHT = 200
# 弹窗整体高度上限（定位夹取的兜底近似值）。
_MAX_HEIGHT = _MAX_TEXT_HEIGHT + 70
# 宽度下界（像素）：再短的译文也要容纳底部「复制 ×」小按钮行。
_MIN_WIDTH = 120
# 文本区左右余量（卡片 padding 28 + QTextEdit 文档边距近似 10）。
_TEXT_MARGIN_H = 38
# 相对光标的默认偏移（右下方），避免遮挡选中文字。
_CURSOR_OFFSET_X = 12
_CURSOR_OFFSET_Y = 16


class SelectionPopup(QWidget):
    """划词翻译悬浮卡片：极简 result / error 两态结果弹窗。

    不变量：
    - 显示本身不写剪贴板、不抢焦点；只有用户点击「复制」才写剪贴板。
    - 自动关闭计时单发；任何用户交互（鼠标按下 / 复制点击）重置计时。
    - show_* 重复调用更新内容、按新光标位置重新定位并重置计时。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        auto_close_seconds: int = _DEFAULT_AUTO_CLOSE_S,
    ) -> None:
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        super().__init__(parent, flags)
        self.setWindowTitle("DictaShift 划词翻译")
        # 根窗口透明：圆角深色背景由内部 card 容器（#popupCard）的 stylesheet 绘制。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 显示时不抢焦点：用户打完词还要继续操作，弹窗不得打断当前输入焦点。
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # 当前状态："result" | "error"（初始未展示视为 result 空态）。
        self._state: str = "result"

        # 根 layout margins=0，只装一个 card 容器。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = QWidget(self)
        card.setObjectName("popupCard")
        card.setStyleSheet(
            popup_card_stylesheet(
                "popupCard",
                bg=CARD_BG_POPUP,
                radius=11,
                text_bg=None,
                compact_buttons=True,
            )
        )
        root.addWidget(card)

        content = QVBoxLayout(card)
        content.setContentsMargins(14, 10, 14, 10)
        content.setSpacing(6)

        self._text_edit = QTextEdit(card)
        self._text_edit.setReadOnly(True)
        self._text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_font = self._text_edit.font()
        text_font.setPointSize(10)
        self._text_edit.setFont(text_font)
        content.addWidget(self._text_edit)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)
        btn_row.addStretch(1)

        self._copy_btn = QPushButton("复制", card)
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setIconSize(QSize(12, 12))
        self._apply_copy_icon(copied=False)
        self._copy_btn.clicked.connect(self._on_copy_clicked)

        self._close_btn = QPushButton("×", card)
        self._close_btn.setProperty("variant", "ghost")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self.hide)

        btn_row.addWidget(self._copy_btn)
        btn_row.addWidget(self._close_btn)
        content.addLayout(btn_row)

        # 自动关闭计时：单次触发。
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.setInterval(int(auto_close_seconds) * 1000)
        self._auto_close_timer.timeout.connect(self.hide)

        # 「已复制」回显计时：到期恢复按钮文字。
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(_COPIED_FEEDBACK_MS)
        self._copied_timer.timeout.connect(self._reset_copy_button_text)

    # ==================== 公共接口 ====================

    def show_result(self, text: str) -> None:
        """显示译文：可复制，按光标位置显示并重置计时。"""
        self._show(text, state="result")

    def show_error(self, message: str) -> None:
        """以错误形式展示简短消息（可复制错误文本）。"""
        self._show(message, state="error")

    def set_auto_close_seconds(self, seconds: int) -> None:
        """更新自动关闭秒数；计时进行中立即按新值重启（设置应用后即时生效）。"""
        self._auto_close_timer.setInterval(int(seconds) * 1000)
        if self._auto_close_timer.isActive():
            self._auto_close_timer.start()

    # ==================== 内部：展示 ====================

    def _apply_copy_icon(self, *, copied: bool) -> None:
        icon = (
            get_icon("check", color=SUCCESS)
            if copied
            else get_icon("copy", color=TEXT_SECONDARY, color_active=TEXT_PRIMARY)
        )
        if not icon.isNull():
            self._copy_btn.setIcon(icon)

    def _show(self, text: str, state: str) -> None:
        self._state = state
        self._text_edit.setPlainText(text)
        self._reset_copy_button_text()
        self._adjust_size_to_content()
        self._place_near_cursor()
        self.show()
        self.raise_()
        # 不 activateWindow：WA_ShowWithoutActivating 下保持当前输入焦点。
        self._auto_close_timer.start()

    # ==================== 内部：尺寸自适应 ====================

    def _adjust_size_to_content(self) -> None:
        fm = self._text_edit.fontMetrics()
        text = self._text_edit.toPlainText()
        lines = text.splitlines() or [""]
        natural_text_w = max(fm.horizontalAdvance(line) for line in lines)
        width = max(_MIN_WIDTH, min(natural_text_w + _TEXT_MARGIN_H, _MAX_WIDTH))
        self.setFixedWidth(width)

        # 使用独立 clone 的 QTextDocument 测量实际换行高度，避免未 show 前
        # QTextEdit viewport 默认窄宽度覆盖 setTextWidth 导致高度虚高。
        doc_clone = self._text_edit.document().clone()
        doc_clone.setDefaultFont(self._text_edit.font())
        doc_clone.setTextWidth(max(1.0, float(width - _TEXT_MARGIN_H + 8)))
        text_h = int(doc_clone.size().height()) + 6
        self._text_edit.setFixedHeight(min(text_h, _MAX_TEXT_HEIGHT))

        self.adjustSize()

    # ==================== 内部：复制 ====================

    def _on_copy_clicked(self) -> None:
        text = self._text_edit.toPlainText()
        try:
            set_clipboard_text(text)
        except OutputError:
            self._copy_btn.setText("复制失败")
            self._copied_timer.start()
        else:
            self._copy_btn.setText("已复制 ✓")
            self._apply_copy_icon(copied=True)
            self._copied_timer.start()
        self._auto_close_timer.start()

    def _reset_copy_button_text(self) -> None:
        self._copy_btn.setText("复制")
        self._apply_copy_icon(copied=False)

    # ==================== 内部：定位 ====================

    def _place_near_cursor(self, pos: QPoint | None = None) -> None:
        if pos is None:
            pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.move(pos.x() + _CURSOR_OFFSET_X, pos.y() + _CURSOR_OFFSET_Y)
            return

        geo = screen.availableGeometry()
        w = self.width() or self.sizeHint().width() or _MAX_WIDTH
        h = self.height() or self.sizeHint().height() or _MAX_HEIGHT
        w = min(w, _MAX_WIDTH)
        h = min(h, _MAX_HEIGHT)

        x = pos.x() + _CURSOR_OFFSET_X
        y = pos.y() + _CURSOR_OFFSET_Y
        if x + w > geo.right():
            x = geo.right() - w
        if y + h > geo.bottom():
            y = geo.bottom() - h
        if x < geo.left():
            x = geo.left()
        if y < geo.top():
            y = geo.top()
        self.move(x, y)

    # ==================== 用户交互重置自动关闭计时 ====================

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._auto_close_timer.start()
        super().mousePressEvent(event)
