"""划词翻译悬浮卡片：跟随光标的取词翻译结果弹窗。

行为 (任务规格)：
- 无边框、置顶、工具窗口（FramelessWindowHint | StaysOnTopHint | Tool），
  半透明圆角深色卡片，与 result_popup/overlay 同风格（配色常量见 theme.py）。
- WA_ShowWithoutActivating + 不 activateWindow：显示时不抢焦点，
  用户划词后可继续在当前输入焦点上操作。
- 三个状态：loading（「翻译中…」，复制按钮禁用）/ result（译文，可复制）/
  error（简短错误提示，可复制错误文本）。
- 位置：显示时取 QCursor.pos() 偏移到光标右下方，用 QGuiApplication.screenAt
  拿所在屏幕几何并夹取边界（防出屏）。
- QTimer 单发自动关闭（秒数由构造参数传入，默认 10；set_auto_close_seconds
  运行时更新）；复制/鼠标按下等用户交互重置计时。
- 重复触发：show_* 系列在已显示时替换内容、重置状态与计时器、
  按新光标位置重新定位。

线程模型：纯 UI 主线程；由 application 层经 queued signal 调用 show_* 系列方法。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..output.clipboard import OutputError, set_clipboard_text
from .theme import (
    CARD_BG_POPUP,
    CARD_TEXT,
    POPUP_BTN_BG,
    POPUP_BTN_BORDER,
    POPUP_BTN_HOVER,
    POPUP_BTN_PRESSED,
    POPUP_TEXT_BG,
    POPUP_TEXT_DARK,
)


# 默认自动关闭秒数（构造参数缺省值；设置页可调 5~60）。
_DEFAULT_AUTO_CLOSE_S = 10
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
# 相对光标的默认偏移（右下方），避免遮挡选中文字。
_CURSOR_OFFSET_X = 12
_CURSOR_OFFSET_Y = 16

# 三状态文案。
_LOADING_TEXT = "翻译中…"
_TITLE_RESULT = "划词翻译"
_TITLE_ERROR = "出错了"


class SelectionPopup(QWidget):
    """划词翻译悬浮卡片：loading / result / error 三态结果弹窗。

    不变量：
    - 显示本身不写剪贴板、不抢焦点；只有用户点击「复制」才写剪贴板。
    - 自动关闭计时单发；任何用户交互（鼠标按下 / 复制点击）重置计时。
    - show_* 重复调用更新内容与状态、按新光标位置重新定位并重置计时。
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
        self.setMaximumWidth(_MAX_WIDTH)
        self.setMaximumHeight(_MAX_HEIGHT)
        # 根窗口透明：圆角深色背景由内部 card 容器（#popupCard）的 stylesheet 绘制。
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 显示时不抢焦点：用户打完词还要继续操作，弹窗不得打断当前输入焦点。
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # 当前状态："loading" | "result" | "error"（初始未展示视为 result 空态）。
        self._state: str = "result"

        # 根 layout margins=0，只装一个 card 容器（与 ResultPopup 同结构）。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        card = QWidget(self)
        card.setObjectName("popupCard")
        card.setStyleSheet(
            f"#popupCard {{ background-color: {CARD_BG_POPUP}; border-radius: 12px; }}"
            f" QLabel {{ color: {CARD_TEXT}; background: transparent; }}"
            f" QTextEdit {{ background-color: {POPUP_TEXT_BG}; color: {POPUP_TEXT_DARK};"
            " border: none; border-radius: 8px; }"
            f" QPushButton {{ background-color: {POPUP_BTN_BG}; color: {CARD_TEXT};"
            f" border: 1px solid {POPUP_BTN_BORDER}; border-radius: 6px; padding: 3px 12px; }}"
            f" QPushButton:hover {{ background-color: {POPUP_BTN_HOVER}; }}"
            f" QPushButton:pressed {{ background-color: {POPUP_BTN_PRESSED}; }}"
        )
        root.addWidget(card)

        content = QVBoxLayout(card)
        content.setContentsMargins(12, 10, 12, 10)
        content.setSpacing(6)

        self._title = QLabel(_TITLE_RESULT, card)
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
        self._auto_close_timer.setInterval(int(auto_close_seconds) * 1000)
        self._auto_close_timer.timeout.connect(self.hide)

        # 「已复制」回显计时：到期恢复按钮文字。
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(_COPIED_FEEDBACK_MS)
        self._copied_timer.timeout.connect(self._reset_copy_button_text)

    # ==================== 公共接口 ====================

    def show_loading(self) -> None:
        """进入「翻译中…」状态：复制按钮禁用，按光标位置显示并重置计时。"""
        self._show(_LOADING_TEXT, _TITLE_RESULT, state="loading")

    def show_result(self, text: str) -> None:
        """显示译文：可复制，按光标位置显示并重置计时。"""
        self._show(text, _TITLE_RESULT, state="result")

    def show_error(self, message: str) -> None:
        """以错误形式展示消息：标题「出错了」，文本区显示 message（可复制）。"""
        self._show(message, _TITLE_ERROR, state="error")

    def set_auto_close_seconds(self, seconds: int) -> None:
        """更新自动关闭秒数；计时进行中立即按新值重启（设置应用后即时生效）。"""
        self._auto_close_timer.setInterval(int(seconds) * 1000)
        if self._auto_close_timer.isActive():
            self._auto_close_timer.start()

    # ==================== 内部：展示 ====================

    def _show(self, text: str, title: str, state: str) -> None:
        """公共展示逻辑：更新状态/标题/文本、自适应尺寸、按光标定位、显示并重置计时。

        重复调用（含已显示时）替换内容并重新定位到新光标位置。
        loading 状态禁用复制按钮（避免复制「翻译中…」占位文案）。
        """
        self._state = state
        self._title.setText(title)
        self._text_edit.setPlainText(text)
        self._copy_btn.setEnabled(state != "loading")
        self._reset_copy_button_text()
        self._adjust_size_to_content()
        self._place_near_cursor()
        self.show()
        self.raise_()
        # 不 activateWindow：WA_ShowWithoutActivating 下保持当前输入焦点。
        self._auto_close_timer.start()

    # ==================== 内部：尺寸自适应 ====================

    def _adjust_size_to_content(self) -> None:
        """按当前文本内容重算窗口宽度与文本区高度（与 ResultPopup 同策略）。

        - 宽：取文本各行 horizontalAdvance 最大值 + 文本区左右余量，clamp 到
          [_MIN_WIDTH, _MAX_WIDTH]，setFixedWidth 到该值（保留 setMaximumWidth(420)）。
        - 文本区高：按行数 clamp 到 [_MIN_TEXT_HEIGHT, _MAX_TEXT_HEIGHT]，
          超出由 QTextEdit 内部滚动。
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
        # 宽度已由 _adjust_size_to_content 锁定；高度用 sizeHint 近似（布局决定）。
        w = self.width() or self.sizeHint().width() or _MAX_WIDTH
        h = self.sizeHint().height() or _MAX_HEIGHT
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
