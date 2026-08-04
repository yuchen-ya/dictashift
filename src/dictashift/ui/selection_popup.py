"""划词翻译悬浮卡片：极简深色圆角卡片，直接显示译文。

行为 (任务规格)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool），
  半透明圆角深色卡片，与 result_popup/overlay 同风格（配色常量见 theme.py）。
- 极简单卡：无标题栏、无独立文本编辑区底色；译文直接画在深色卡片上
  （QTextEdit 透明无边框，仅利用其换行/滚动/选中能力），紧 padding。
- 只在有最终结果时出现：对外 API 收敛为 show_result / show_error
  （无 loading 态——取词成功后弹窗不抢先出现，翻译完成才弹出）。
- 尺寸随内容自适应：宽度按文本自然宽度收缩（短译文就是一小条），
  超 _MAX_WIDTH 自动换行；文本高度按行数增长，超 _MAX_TEXT_HEIGHT 紧凑滚动。
  禁止大面积空白。
- WA_ShowWithoutActivating + 不 activateWindow：显示时不抢焦点，
  用户划词后可继续在当前输入焦点上操作。
- 位置：显示时取 QCursor.pos() 偏移到光标右下方，用 QGuiApplication.screenAt
  拿所在屏幕几何并夹取边界（防出屏）。
- QTimer 单发自动关闭（秒数由构造参数传入，默认 10；set_auto_close_seconds
  运行时更新）；复制/鼠标按下等用户交互重置计时。
- 重复触发：show_* 在已显示时替换内容、重置计时、按新光标位置重新定位。

线程模型：纯 UI 主线程；由 application 层经 queued signal 调用 show_* 方法。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..output.clipboard import OutputError, set_clipboard_text
from .theme import (
    ACCENT,
    CARD_BG_POPUP,
    CARD_TEXT,
    POPUP_BTN_BG,
    POPUP_BTN_BORDER,
    POPUP_BTN_HOVER,
    POPUP_BTN_PRESSED,
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
# 文本区左右余量（卡片 padding 24 + QTextEdit 文档边距近似 8）。
_TEXT_MARGIN_H = 32
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
            f"#popupCard {{ background-color: {CARD_BG_POPUP}; border-radius: 10px; }}"
            # 文本区透明无边框：译文直接显示在深色卡片上，选中态用 accent 紫。
            f" QTextEdit {{ background: transparent; border: none; color: {CARD_TEXT};"
            f" selection-background-color: {ACCENT}; }}"
            # 紧凑滚动条：细长、无箭头按钮。
            " QScrollBar:vertical { width: 8px; background: transparent; margin: 0; }"
            f" QScrollBar::handle:vertical {{ background: {POPUP_BTN_BORDER};"
            " border-radius: 4px; min-height: 20px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            # 小号按钮：紧 padding，不撑出大片留白。
            f" QPushButton {{ background-color: {POPUP_BTN_BG}; color: {CARD_TEXT};"
            f" border: 1px solid {POPUP_BTN_BORDER}; border-radius: 5px; padding: 1px 8px; }}"
            f" QPushButton:hover {{ background-color: {POPUP_BTN_HOVER}; }}"
            f" QPushButton:pressed {{ background-color: {POPUP_BTN_PRESSED}; }}"
        )
        root.addWidget(card)

        content = QVBoxLayout(card)
        content.setContentsMargins(12, 8, 12, 8)
        content.setSpacing(4)

        self._text_edit = QTextEdit(card)
        self._text_edit.setReadOnly(True)
        self._text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 正文字号：与 overlay 正文同级（10pt），保证清晰可读，不用小字。
        text_font = self._text_edit.font()
        text_font.setPointSize(10)
        self._text_edit.setFont(text_font)
        # 文本区高度在 _show 里按内容行数自适应（封顶 _MAX_TEXT_HEIGHT）。
        content.addWidget(self._text_edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        btn_row.addStretch(1)
        self._copy_btn = QPushButton("复制", card)
        self._copy_btn.clicked.connect(self._on_copy_clicked)
        self._close_btn = QPushButton("×", card)
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

    def _show(self, text: str, state: str) -> None:
        """公共展示逻辑：更新状态/文本、自适应尺寸、按光标定位、显示并重置计时。

        重复调用（含已显示时）替换内容并重新定位到新光标位置。
        """
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
        """按当前文本内容重算窗口宽度与文本区高度（短译文就是一小条）。

        - 宽：取文本各行 horizontalAdvance 最大值 + 文本区左右余量，clamp 到
          [_MIN_WIDTH, _MAX_WIDTH]，setFixedWidth 到该值。
        - 文本区高：按该宽度下实际换行后的文档高度计算，封顶 _MAX_TEXT_HEIGHT，
          超出由 QTextEdit 内部紧凑滚动。
        """
        fm = self._text_edit.fontMetrics()
        text = self._text_edit.toPlainText()
        lines = text.splitlines() or [""]
        natural_text_w = max(fm.horizontalAdvance(line) for line in lines)
        width = max(_MIN_WIDTH, min(natural_text_w + _TEXT_MARGIN_H, _MAX_WIDTH))
        self.setFixedWidth(width)

        # 按实际排版宽度取文档高度（含自动换行），而非原始行数。
        doc = self._text_edit.document()
        doc.setTextWidth(width - _TEXT_MARGIN_H)
        text_h = int(doc.size().height()) + 4  # 上下余量近似
        self._text_edit.setFixedHeight(min(text_h, _MAX_TEXT_HEIGHT))

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

    def _place_near_cursor(self, pos: QPoint | None = None) -> None:
        """把窗口定位到光标右下方，夹取在光标所在屏幕的可用几何内（防出屏）。

        pos 默认取 QCursor.pos()；测试可传显式位置（offscreen 下不移动真实光标）。
        右/下侧越界时收回到屏幕内，仍越界（窗口比屏幕大）则贴左/上边界。
        """
        if pos is None:
            pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            # 无屏幕可用（极端 offscreen）：直接按偏移定位，不做夹取。
            self.move(pos.x() + _CURSOR_OFFSET_X, pos.y() + _CURSOR_OFFSET_Y)
            return

        geo = screen.availableGeometry()
        # 宽度/高度已由 _adjust_size_to_content 锁定（adjustSize 后 height() 准确；
        # sizeHint 可能小于实际高度——布局 minimumSize 兜底），退而求其次用 sizeHint。
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
        # 任意鼠标按下视为用户活跃，重置自动关闭。
        self._auto_close_timer.start()
        super().mousePressEvent(event)
