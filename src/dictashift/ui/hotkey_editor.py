"""快捷键编辑控件：QKeySequenceEdit 的封装 + 配置字符串双向转换。

配置格式（与 dictashift.hotkeys.matcher.parse_hotkey 兼容）：
- 小写、'+' 连接，如 "ctrl+alt+f12"、"f8"、"esc"。
- 修饰键统一为 ctrl/alt/shift/cmd；普通键取小写形式（F8 -> f8）。

QKeySequence 文本格式（Qt 默认 PortableSeries）：
- 修饰键形如 "Ctrl"、"Alt"、"Shift"、"Meta"；普通键如 "F8"、"Esc"、"A"。
- 多键组合用 "+" 连接，如 "Ctrl+Alt+F12"。

线程模型：本控件纯 UI 主线程使用，无后台线程。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QPushButton,
    QWidget,
)

from .icons import get_icon
from .theme import TEXT_PRIMARY, TEXT_SECONDARY

# QKeySequence 文本中的修饰键前缀 -> 配置格式名称。
# 顺序无关：我们按 '+' 拆分后逐个 token 映射。
_MOD_MAP = {
    "ctrl": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "meta": "cmd",  # Qt 用 Meta 表示 Windows/Cmd 键
}


def config_to_sequence(text: str) -> QKeySequence:
    """把配置字符串（"ctrl+alt+f12"）转换为 QKeySequence。

    空字符串返回空 QKeySequence。
    未知 token 按原样大写首字母尝试（兼容 Qt 的 key 名）；解析失败的 token 忽略。
    """
    if not text or not text.strip():
        return QKeySequence()
    parts = [p.strip() for p in text.split("+") if p.strip()]
    qt_tokens: list[str] = []
    for p in parts:
        low = p.lower()
        if low in _MOD_MAP:
            # 修饰键映射为 Qt 文本形式：Ctrl/Alt/Shift/Meta
            qt_name = {
                "ctrl": "Ctrl",
                "alt": "Alt",
                "shift": "Shift",
                "cmd": "Meta",
            }[_MOD_MAP[low]]
            qt_tokens.append(qt_name)
            continue
        # 普通键：单字符直接大写；功能键/特殊键首字母大写（f8 -> F8, esc -> Esc）。
        if len(low) == 1:
            qt_tokens.append(low.upper())
        else:
            qt_tokens.append(low.capitalize())
    return QKeySequence("+".join(qt_tokens))


def sequence_to_config(seq: QKeySequence) -> str:
    """把 QKeySequence 转换为配置字符串（"ctrl+alt+f12"）。

    空序列返回 ""。
    修饰键 Ctrl/Alt/Shift/Meta 映射为 ctrl/alt/shift/cmd；其余 token 小写。
    """
    if seq is None or seq.isEmpty():
        return ""
    # toString() 默认返回 "Ctrl+Alt+F12" 形式（PortableSeries）。
    raw = seq.toString()
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    out: list[str] = []
    for p in parts:
        low = p.lower()
        if low in _MOD_MAP:
            out.append(_MOD_MAP[low])
        else:
            out.append(low)
    return "+".join(out)


class HotkeyEdit(QWidget):
    """现代胶囊风格快捷键编辑控件：QKeySequenceEdit + 矢量图标清除按钮。

    用法：
        edit = HotkeyEdit()
        edit.set_config_text("ctrl+alt+f12")
        cfg = edit.config_text()  # "ctrl+alt+f12"
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("hotkeyEdit")

        self._seq_edit = QKeySequenceEdit(self)
        self._seq_edit.setMinimumWidth(148)
        self._seq_edit.setFixedHeight(32)

        self._clear_btn = QPushButton("", self)
        self._clear_btn.setProperty("variant", "icon")
        self._clear_btn.setToolTip("清除快捷键")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setFixedSize(32, 32)
        clear_icon = get_icon("clear", color=TEXT_SECONDARY, color_active=TEXT_PRIMARY)
        if not clear_icon.isNull():
            self._clear_btn.setIcon(clear_icon)
            self._clear_btn.setIconSize(QSize(14, 14))
        else:
            self._clear_btn.setText("×")
        self._clear_btn.clicked.connect(self._on_clear)

        layout: QBoxLayout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._seq_edit, stretch=1)
        layout.addWidget(self._clear_btn)

    def _on_clear(self) -> None:
        self._seq_edit.clear()

    def set_config_text(self, text: str) -> None:
        """用配置字符串设置当前快捷键。"""
        self._seq_edit.setKeySequence(config_to_sequence(text))

    def config_text(self) -> str:
        """返回当前快捷键的配置字符串；未设置返回 ""。"""
        return sequence_to_config(self._seq_edit.keySequence())
