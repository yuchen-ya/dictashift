"""快速查词居中浮窗（极简两段式 Spotlight 暗色风格）。

布局与交互：
- 无边框、置顶、工具窗口（FramelessWindowHint | WindowStaysOnTopHint | Tool），
  复用 theme.py 统一暗色卡片 Token 与 icons.py 的 Phosphor 矢量图标。
- 宽 520px，仅保留两层极简结构：
  1. 顶栏输入条（约 48-50px）：左侧搜索图标 + 无边框输入框 + 仅非空时显示的清空按钮 +
     右侧紧凑方向循环胶囊按钮（点击循环 `自动` → `中→英` → `英→中` → `自动`）。
  2. 结果行（空白空闲态完全隐藏，使浮窗收起为一条利落的 Spotlight 搜索栏；
     翻译中/有译文/出错时在 1px 细分割线下方展开）：左侧译文直接铺在卡片底面上
     （无嵌套边框盒，可选中复制，长文本超限平滑滚动），右侧仅保留一个紧凑动作按钮
     （译文态「复制」→ 1.5s「已复制」反馈；错误态「重试」；加载态隐藏）。
- 防抖与立即翻译：输入停顿 450ms 自动翻译；Enter 取消防抖立即翻译；
  清空文本立即取消防抖、递增序号并收起结果行；点击切换方向时非空立即重译。
- 请求序号防乱序：弹窗维护递增 `_request_seq`，仅接收匹配最新序号的
  `show_result` / `show_error`，自动丢弃过期响应。
- 关闭方式：点击外部失焦自动关闭（含 250ms 唤起宽限期）、按 Esc 关闭或再按热键 toggle。

线程模型：纯 UI 主线程；网络翻译由 application 层在后台线程执行。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..output.clipboard import OutputError, set_clipboard_text
from ..translation.langdetect import _is_cjk, detect_direction
from .icons import get_icon, get_pixmap
from .theme import (
    ACCENT,
    ACCENT_BORDER,
    ACCENT_SOFT,
    CARD_BG_POPUP,
    DANGER_TEXT,
    SUCCESS,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    popup_card_stylesheet,
)

logger = logging.getLogger(__name__)

# ==================== 常量 ====================

_POPUP_WIDTH = 520
_MIN_RESULT_HEIGHT = 24
_MAX_RESULT_HEIGHT = 220
_DEBOUNCE_MS = 450
_COPIED_FEEDBACK_MS = 1_500
_DEACTIVATE_GRACE_MS = 250

_DIRECTION_CYCLE: tuple[str, ...] = ("auto", "zh2en", "en2zh")
_MODE_PILL_LABELS: dict[str, str] = {
    "auto": "自动",
    "zh2en": "中→英",
    "en2zh": "英→中",
}


# ==================== 纯函数：方向解析 ====================


def resolve_lookup_direction(text: str, mode: str = "auto") -> str:
    """根据查词方向模式与输入文本解析实际翻译方向（``"zh2en"`` / ``"en2zh"``）。

    规则：
    - ``mode == "zh2en"`` -> ``"zh2en"``
    - ``mode == "en2zh"`` -> ``"en2zh"``
    - ``mode == "auto"``（或未知值）-> 复用 ``detect_direction(text)``，偏向中译英：
      若输入不含任何英文字母或含任意中文 CJK 字符则返回 ``"zh2en"``，
      明确纯英文/含拉丁字母且不含中文时返回 ``"en2zh"``。
    """
    if mode == "zh2en":
        return "zh2en"
    if mode == "en2zh":
        return "en2zh"
    detected = detect_direction(text)
    if detected == "zh2en":
        return "zh2en"
    has_cjk = any(_is_cjk(ch) for ch in text)
    has_latin = any(("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in text)
    if has_cjk or not has_latin:
        return "zh2en"
    return "en2zh"


# ==================== Windows 前台聚焦辅助（64 位安全 ctypes，可 mock） ====================

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.AttachThreadInput.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    ]
    _user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    _user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetFocus.restype = wintypes.HWND
    _user32.SetFocus.argtypes = [wintypes.HWND]

    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentThreadId.argtypes = []
else:  # pragma: no cover
    _user32 = None
    _kernel32 = None


def _focus_window_windows(hwnd: int) -> bool:
    """在 Windows 真实 GUI 环境下尽力将浮窗自身置前并获取输入焦点（绕过 Foreground Lock）。

    所有 Win32 API 均已在模块级显式声明 argtypes/restype（使用 wintypes.HWND），
    确保 64 位大句柄不溢出。非 Windows、offscreen/minimal 平台或无效句柄安全返回 False。
    """
    if _user32 is None or _kernel32 is None or not hwnd:
        return False
    try:
        platform = QGuiApplication.platformName().lower()
        if platform in ("offscreen", "minimal", ""):
            return False

        fg_hwnd = _user32.GetForegroundWindow()
        cur_tid = _kernel32.GetCurrentThreadId()
        fg_tid = _user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0

        _user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_user32.AttachThreadInput(fg_tid, cur_tid, True))
        try:
            _user32.BringWindowToTop(hwnd)
            ok = bool(_user32.SetForegroundWindow(hwnd))
            _user32.SetFocus(hwnd)
            return ok
        finally:
            if attached:
                _user32.AttachThreadInput(fg_tid, cur_tid, False)
    except Exception:  # noqa: BLE001
        logger.debug("[QuickLookupPopup] Windows 前台聚焦降级跳过", exc_info=True)
        return False


# ==================== 卡片定向样式 ====================


def _quick_lookup_stylesheet() -> str:
    base = popup_card_stylesheet(
        "quickLookupCard",
        bg=CARD_BG_POPUP,
        radius=14,
        text_bg=None,
        text_border=None,
        compact_buttons=True,
    )
    extra = f"""
#quickLookupCard {{
    background-color: {CARD_BG_POPUP};
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 14px;
}}
#quickLookupCard QWidget#lookupInputRow,
#quickLookupCard QWidget#lookupResultRow {{
    background-color: transparent;
}}
#quickLookupCard QLineEdit#lookupInput {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 3px 2px;
    color: {TEXT_PRIMARY};
    font-size: 15px;
    font-weight: 500;
    selection-background-color: rgba(124, 108, 246, 0.34);
    selection-color: {TEXT_PRIMARY};
}}
#quickLookupCard QLineEdit#lookupInput:focus {{
    border: none;
    background-color: transparent;
}}
#quickLookupCard QFrame#lookupDivider {{
    background-color: rgba(255, 255, 255, 0.08);
    border: none;
    min-height: 1px;
    max-height: 1px;
}}
#quickLookupCard QPushButton#lookupClearBtn {{
    background-color: transparent;
    border: none;
    border-radius: 12px;
    padding: 0px;
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
}}
#quickLookupCard QPushButton#lookupClearBtn:hover {{
    background-color: rgba(255, 255, 255, 0.08);
}}
#quickLookupCard QPushButton#lookupDirBtn {{
    background-color: rgba(255, 255, 255, 0.06);
    color: {TEXT_SECONDARY};
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 7px;
    padding: 3px 10px;
    min-height: 20px;
    font-size: 12px;
    font-weight: 500;
}}
#quickLookupCard QPushButton#lookupDirBtn:hover {{
    background-color: rgba(255, 255, 255, 0.11);
    color: {TEXT_PRIMARY};
    border-color: rgba(255, 255, 255, 0.16);
}}
#quickLookupCard QPushButton#lookupDirBtn[locked="true"] {{
    background-color: {ACCENT_SOFT};
    color: #CFC8FF;
    border: 1px solid {ACCENT_BORDER};
}}
#quickLookupCard QPushButton#lookupDirBtn[locked="true"]:hover {{
    background-color: rgba(124, 108, 246, 0.24);
    color: #FFFFFF;
}}
#quickLookupCard QTextEdit#lookupResultEdit {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
    color: {TEXT_PRIMARY};
    font-size: 14px;
    selection-background-color: {ACCENT};
}}
#quickLookupCard QLabel#lookupLoadingLabel {{
    color: {TEXT_SECONDARY};
    font-size: 13.5px;
    padding: 1px 0px;
}}
#quickLookupCard QLabel#lookupErrorLabel {{
    color: {DANGER_TEXT};
    font-size: 13.5px;
    padding: 1px 0px;
}}
#quickLookupCard QPushButton#lookupActionBtn {{
    background-color: rgba(255, 255, 255, 0.06);
    color: {TEXT_PRIMARY};
    border: 1px solid rgba(255, 255, 255, 0.11);
    border-radius: 7px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
}}
#quickLookupCard QPushButton#lookupActionBtn:hover {{
    background-color: rgba(255, 255, 255, 0.12);
    border-color: rgba(255, 255, 255, 0.20);
}}
#quickLookupCard QPushButton#lookupActionBtn:pressed {{
    background-color: rgba(255, 255, 255, 0.18);
}}
"""
    return base + extra


# ==================== QuickLookupPopup ====================


class QuickLookupPopup(QWidget):
    """极简两段式 Spotlight 居中快速查词浮窗。

    信号：
    - ``translate_requested(request_id, text, direction)``：请求后台翻译。
    - ``request_cancelled(seq)``：输入被清空或浮窗隐藏，通知上层使在途翻译失效。
    - ``visibility_changed(visible)``：浮窗显隐状态变化，供上层抑制全局 Esc 取消穿透。
    """

    translate_requested = Signal(int, str, str)
    request_cancelled = Signal(int)
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        super().__init__(parent, flags)
        self.setObjectName("quickLookupRoot")
        self.setWindowTitle("DictaShift 快速查词")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("QWidget#quickLookupRoot { background: transparent; }")
        self.setFixedWidth(_POPUP_WIDTH)

        # 状态
        self._request_seq: int = 0
        self._current_result: str = ""
        self._direction_mode: str = "auto"
        self._opened_at: float = 0.0
        self._drag_offset: QPoint | None = None

        # 根布局（透明外框，圆角与 1px 描边由内部 QFrame 卡片绘制）
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._card = QFrame(self)
        self._card.setObjectName("quickLookupCard")
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card.setStyleSheet(_quick_lookup_stylesheet())
        root.addWidget(self._card)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # ---- Tier 1：极简顶部输入栏（单行 ~48px）----
        self._input_row = QWidget(self._card)
        self._input_row.setObjectName("lookupInputRow")
        input_layout = QHBoxLayout(self._input_row)
        input_layout.setContentsMargins(16, 10, 12, 10)
        input_layout.setSpacing(10)

        self._search_icon = QLabel(self._input_row)
        self._search_icon.setFixedSize(17, 17)
        search_pm = get_pixmap("search", color=TEXT_SECONDARY, size=17)
        if not search_pm.isNull():
            self._search_icon.setPixmap(search_pm)
        input_layout.addWidget(self._search_icon)

        self._input = QLineEdit(self._input_row)
        self._input.setObjectName("lookupInput")
        self._input.setPlaceholderText("输入中文或英文...")
        self._input.setMinimumHeight(28)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.returnPressed.connect(self._on_return_pressed)
        self._input.installEventFilter(self)
        input_layout.addWidget(self._input, stretch=1)

        self._clear_btn = QPushButton("", self._input_row)
        self._clear_btn.setObjectName("lookupClearBtn")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setToolTip("清空")
        self._clear_btn.setIconSize(QSize(13, 13))
        clear_icon = get_icon("clear", color=TEXT_MUTED, color_active=TEXT_PRIMARY)
        if not clear_icon.isNull():
            self._clear_btn.setIcon(clear_icon)
        else:
            self._clear_btn.setText("×")
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        self._clear_btn.hide()
        input_layout.addWidget(self._clear_btn)

        self._dir_btn = QPushButton("自动", self._input_row)
        self._dir_btn.setObjectName("lookupDirBtn")
        self._dir_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dir_btn.setToolTip("点击切换翻译方向：自动 → 中→英 → 英→中")
        self._dir_btn.clicked.connect(self._cycle_direction_mode)
        self._sync_dir_button()
        input_layout.addWidget(self._dir_btn)

        card_layout.addWidget(self._input_row)

        # ---- 1px 细分割线（空闲态隐藏）----
        self._result_divider = QFrame(self._card)
        self._result_divider.setObjectName("lookupDivider")
        self._result_divider.setFrameShape(QFrame.Shape.HLine)
        card_layout.addWidget(self._result_divider)

        # ---- Tier 2：极简结果行（空闲态完全隐藏；无嵌套边框盒）----
        self._result_container = QWidget(self._card)
        self._result_container.setObjectName("lookupResultRow")
        result_layout = QHBoxLayout(self._result_container)
        result_layout.setContentsMargins(16, 12, 12, 13)
        result_layout.setSpacing(12)

        self._loading_label = QLabel("翻译中…", self._result_container)
        self._loading_label.setObjectName("lookupLoadingLabel")
        self._loading_label.hide()
        result_layout.addWidget(self._loading_label, stretch=1)

        self._error_label = QLabel("", self._result_container)
        self._error_label.setObjectName("lookupErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._error_label.hide()
        result_layout.addWidget(self._error_label, stretch=1)

        self._result_edit = QTextEdit(self._result_container)
        self._result_edit.setObjectName("lookupResultEdit")
        self._result_edit.setFrameShape(QFrame.Shape.NoFrame)
        self._result_edit.document().setDocumentMargin(0)
        self._result_edit.setReadOnly(True)
        self._result_edit.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._result_edit.installEventFilter(self)
        self._result_edit.hide()
        result_layout.addWidget(self._result_edit, stretch=1)

        # 右侧唯一紧凑操作按钮：译文态「复制」 / 错误态「重试」 / 加载态隐藏
        self._copy_btn = QPushButton("复制", self._result_container)
        self._copy_btn.setObjectName("lookupActionBtn")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setIconSize(QSize(13, 13))
        self._copy_btn.setToolTip("复制译文 (Ctrl+Shift+C)")
        self._copy_btn.clicked.connect(self._on_copy_clicked)
        self._apply_copy_icon(copied=False)
        self._copy_btn.hide()
        result_layout.addWidget(self._copy_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._retry_btn = QPushButton("重试", self._result_container)
        self._retry_btn.setObjectName("lookupActionBtn")
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.setIconSize(QSize(13, 13))
        self._retry_btn.setToolTip("重新翻译")
        retry_icon = get_icon("refresh", color=TEXT_SECONDARY, color_active=TEXT_PRIMARY)
        if not retry_icon.isNull():
            self._retry_btn.setIcon(retry_icon)
        self._retry_btn.clicked.connect(self._trigger_translate_now)
        self._retry_btn.hide()
        result_layout.addWidget(self._retry_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

        card_layout.addWidget(self._result_container)

        # 计时器：输入防抖 + 已复制瞬态回显
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(_DEBOUNCE_MS)
        self._debounce_timer.timeout.connect(self._trigger_translate_now)

        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.setInterval(_COPIED_FEEDBACK_MS)
        self._copied_timer.timeout.connect(self._reset_copy_button)

        # 初始为单行紧凑空白态
        self._collapse_result_area()

    # ==================== 公共接口 ====================

    @property
    def request_seq(self) -> int:
        """当前最新翻译请求序号（供调试与测试校验防乱序）。"""
        return self._request_seq

    @property
    def current_result(self) -> str:
        """当前展示的有效译文（无有效结果时为空串）。"""
        return self._current_result

    def direction_mode(self) -> str:
        """返回当前方向模式（``"auto"`` / ``"zh2en"`` / ``"en2zh"``）。"""
        return self._direction_mode

    def set_direction_mode(self, mode: str) -> None:
        """切换方向模式；非法值回退 ``"auto"``。若输入框非空则立即重译。"""
        normalized = mode if mode in _MODE_PILL_LABELS else "auto"
        changed = normalized != self._direction_mode
        self._direction_mode = normalized
        self._sync_dir_button()
        if changed and self._input.text().strip():
            self._debounce_timer.stop()
            self._trigger_translate_now()

    def open_lookup(self, initial_text: str | None = None) -> None:
        """打开查词浮窗；传入非空 ``initial_text`` 时预填并立即触发翻译。"""
        self._opened_at = time.monotonic()
        self._debounce_timer.stop()
        self._reset_copy_button()

        cleaned = initial_text.strip() if initial_text else ""
        if cleaned:
            was = self._input.blockSignals(True)
            self._input.setText(cleaned)
            self._input.blockSignals(was)
            self._clear_btn.setVisible(True)
            self._trigger_translate_now()
        else:
            was = self._input.blockSignals(True)
            self._input.clear()
            self._input.blockSignals(was)
            self._clear_btn.setVisible(False)
            self._request_seq += 1
            self._collapse_result_area()

        self._place_centered()
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            _focus_window_windows(int(self.winId()))
        except Exception:  # noqa: BLE001
            pass
        self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        if cleaned:
            self._input.selectAll()

    def show_result(
        self,
        request_id: int,
        translated: str,
        actual_direction: str = "",
    ) -> None:
        """回填翻译结果；仅当 ``request_id`` 匹配最新请求序号时生效。"""
        del actual_direction
        if request_id != self._request_seq:
            return
        self._current_result = translated
        self._reset_copy_button()

        self._loading_label.hide()
        self._error_label.hide()
        self._retry_btn.hide()

        self._result_edit.setPlainText(translated)
        self._fit_result_edit_height()
        self._result_edit.show()
        self._copy_btn.show()

        self._result_divider.show()
        self._result_container.show()
        self._sync_window_height()

    def show_error(self, request_id: int, message: str) -> None:
        """回填错误提示；仅当 ``request_id`` 匹配最新请求序号时生效。"""
        if request_id != self._request_seq:
            return
        self._current_result = ""
        self._reset_copy_button()

        self._loading_label.hide()
        self._result_edit.hide()
        self._copy_btn.hide()

        self._error_label.setText(message)
        self._error_label.show()
        self._retry_btn.show()

        self._result_divider.show()
        self._result_container.show()
        self._sync_window_height()

    # ==================== 内部：方向切换与翻译触发 ====================

    def _cycle_direction_mode(self) -> None:
        idx = _DIRECTION_CYCLE.index(self._direction_mode)
        next_mode = _DIRECTION_CYCLE[(idx + 1) % len(_DIRECTION_CYCLE)]
        self.set_direction_mode(next_mode)

    def _sync_dir_button(self) -> None:
        self._dir_btn.setText(_MODE_PILL_LABELS[self._direction_mode])
        self._dir_btn.setProperty(
            "locked", "true" if self._direction_mode != "auto" else "false"
        )
        self._dir_btn.style().unpolish(self._dir_btn)
        self._dir_btn.style().polish(self._dir_btn)

    def _on_text_changed(self, text: str) -> None:
        self._clear_btn.setVisible(bool(text))
        if not text.strip():
            self._debounce_timer.stop()
            self._request_seq += 1
            self.request_cancelled.emit(self._request_seq)
            self._collapse_result_area()
            return
        self._debounce_timer.start()

    def _on_return_pressed(self) -> None:
        self._debounce_timer.stop()
        self._trigger_translate_now()

    def _on_clear_clicked(self) -> None:
        self._input.clear()
        self._input.setFocus(Qt.FocusReason.MouseFocusReason)

    def _trigger_translate_now(self) -> None:
        text = self._input.text().strip()
        if not text:
            self._request_seq += 1
            self._collapse_result_area()
            return

        self._request_seq += 1
        req_id = self._request_seq
        direction = resolve_lookup_direction(text, self._direction_mode)

        self._current_result = ""
        self._reset_copy_button()
        self._error_label.hide()
        self._result_edit.hide()
        self._copy_btn.hide()
        self._retry_btn.hide()
        self._loading_label.show()
        self._result_divider.show()
        self._result_container.show()
        self._sync_window_height()

        self.translate_requested.emit(req_id, text, direction)

    def _collapse_result_area(self) -> None:
        self._current_result = ""
        self._reset_copy_button()
        self._loading_label.hide()
        self._error_label.hide()
        self._result_edit.clear()
        self._result_edit.hide()
        self._copy_btn.hide()
        self._retry_btn.hide()
        self._result_divider.hide()
        self._result_container.hide()
        self._sync_window_height()

    # ==================== 内部：复制动作 ====================

    def _on_copy_clicked(self) -> None:
        if not self._current_result:
            return
        try:
            set_clipboard_text(self._current_result)
        except OutputError:
            self._copy_btn.setText("失败")
            self._copied_timer.start()
        else:
            self._copy_btn.setText("已复制")
            self._apply_copy_icon(copied=True)
            self._copied_timer.start()

    def _reset_copy_button(self) -> None:
        self._copied_timer.stop()
        self._copy_btn.setText("复制")
        self._apply_copy_icon(copied=False)

    def _apply_copy_icon(self, *, copied: bool) -> None:
        icon = (
            get_icon("check", color=SUCCESS)
            if copied
            else get_icon("copy", color=TEXT_SECONDARY, color_active=TEXT_PRIMARY)
        )
        if not icon.isNull():
            self._copy_btn.setIcon(icon)

    # ==================== 内部：尺寸与定位 ====================

    def _fit_result_edit_height(self) -> None:
        doc_clone = self._result_edit.document().clone()
        doc_clone.setDefaultFont(self._result_edit.font())
        doc_clone.setDocumentMargin(0)
        # 520 - 左边距(16) - 右边距(12) - 间距(12) - 右侧复制按钮宽(~72) - 轻微安全余量(8)
        available_w = max(120.0, float(_POPUP_WIDTH - 120))
        doc_clone.setTextWidth(available_w)
        doc_h = int(doc_clone.size().height()) + 4
        clamped = max(_MIN_RESULT_HEIGHT, min(doc_h, _MAX_RESULT_HEIGHT))
        self._result_edit.setFixedHeight(clamped)

    def _sync_window_height(self) -> None:
        self._card.adjustSize()
        self.adjustSize()
        if self.isVisible():
            self._clamp_to_screen()

    def _target_screen(self):
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen

    def _place_centered(self) -> None:
        """定位到鼠标所在屏幕水平居中、垂直约 28% 黄金位置，带防出屏夹取。"""
        screen = self._target_screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        w = self.width() or _POPUP_WIDTH
        h = self.sizeHint().height() or self.height() or 52
        x = geo.left() + (geo.width() - w) // 2
        y = geo.top() + int(geo.height() * 0.28)
        x, y = self._clamp_coords(x, y, w, h, geo)
        self.move(x, y)

    def _clamp_to_screen(self) -> None:
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = self._target_screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x, y = self._clamp_coords(self.x(), self.y(), self.width(), self.height(), geo)
        if x != self.x() or y != self.y():
            self.move(x, y)

    @staticmethod
    def _clamp_coords(x: int, y: int, w: int, h: int, geo) -> tuple[int, int]:
        max_x = max(geo.left(), geo.right() - w + 1)
        max_y = max(geo.top(), geo.bottom() - h + 1)
        x = max(geo.left(), min(x, max_x))
        y = max(geo.top(), min(y, max_y))
        return x, y

    # ==================== 事件处理（快捷键 / 失焦 / 拖拽） ====================

    def _handle_shortcut_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return True
        if (
            key == Qt.Key.Key_C
            and bool(mods & Qt.KeyboardModifier.ControlModifier)
            and bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ):
            if self._current_result:
                self._on_copy_clicked()
            event.accept()
            return True
        return False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched in (self._input, self._result_edit) and event.type() == QEvent.Type.KeyPress:
            if isinstance(event, QKeyEvent) and self._handle_shortcut_key(event):
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._handle_shortcut_key(event):
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowDeactivate:
            if self.isVisible():
                elapsed_ms = (time.monotonic() - self._opened_at) * 1000.0
                if elapsed_ms >= _DEACTIVATE_GRACE_MS:
                    self.hide()
        super().changeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._debounce_timer.stop()
        self._drag_offset = None
        self._request_seq += 1
        self.request_cancelled.emit(self._request_seq)
        self.visibility_changed.emit(False)
        super().hideEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if not isinstance(child, (QLineEdit, QPushButton, QTextEdit)):
                self._drag_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and bool(
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
