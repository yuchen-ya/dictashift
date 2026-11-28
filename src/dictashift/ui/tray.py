"""系统托盘图标。

行为 (任务规格)：
- QSystemTrayIcon，菜单：「快速查词」（支持动态热键文案）、「启用 DictaShift」
  (checkable, 反映 enabled)、「设置…」、「退出」。图标加载 assets/icons 下的真实应用图标，
  缺失时回退 QStyle 标准图标占位。
- 托盘菜单项搭配简洁矢量图标，随全局 QApplication 暗色 QSS 呈现统一现代风格。
- 左键点击托盘图标 = 打开设置窗口。

线程模型：纯 UI 主线程；通过 Qt signal 通知 application 层。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from ..resources import load_app_icon
from .hotkey_editor import config_to_sequence
from .icons import get_icon
from .theme import ACCENT_LIGHT, DANGER_TEXT, SUCCESS, TEXT_MUTED, TEXT_PRIMARY


def _format_hotkey_hint(hotkey_text: str) -> str:
    """将配置热键串（如 ``"alt+q"``）格式化为易读展示文本（如 ``"Alt+Q"``）。"""
    if not hotkey_text or not hotkey_text.strip():
        return ""
    seq = config_to_sequence(hotkey_text)
    rendered = seq.toString() if not seq.isEmpty() else ""
    if rendered:
        return rendered
    return "+".join(part.strip().capitalize() for part in hotkey_text.split("+") if part.strip())


class TrayIcon(QObject):
    """系统托盘图标 + 菜单。

    信号：
    - quick_lookup_requested()：「快速查词」菜单项被点击（直接打开空白查词浮窗）。
    - toggle_enabled_requested()：「启用」菜单项被点击（application 层据此
      调用 controller.handle_hotkey_press("toggle_enabled") 模拟切换）。
    - settings_requested()：「设置…」或左键点击托盘图标，打开设置窗口。
    - quit_requested()：「退出」。
    """

    quick_lookup_requested = Signal()
    toggle_enabled_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(self._load_icon(), self)
        self._tray.setToolTip("DictaShift")

        menu = QMenu()
        self._menu = menu

        self._quick_lookup_action = QAction("快速查词 (Alt+Q)", menu)
        lookup_icon = get_icon("search", color=ACCENT_LIGHT)
        if not lookup_icon.isNull():
            self._quick_lookup_action.setIcon(lookup_icon)
        self._quick_lookup_action.triggered.connect(self.quick_lookup_requested.emit)
        menu.addAction(self._quick_lookup_action)

        menu.addSeparator()

        self._enable_action = QAction("启用 DictaShift", menu)
        self._enable_action.setCheckable(True)
        self._enable_action.setChecked(True)
        self._sync_enable_action_icon(True)
        self._enable_action.triggered.connect(self.toggle_enabled_requested.emit)
        menu.addAction(self._enable_action)

        menu.addSeparator()

        settings_action = QAction("设置…", menu)
        settings_icon = get_icon("settings", color=TEXT_PRIMARY)
        if not settings_icon.isNull():
            settings_action.setIcon(settings_icon)
        settings_action.triggered.connect(self.settings_requested.emit)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("退出", menu)
        quit_icon = get_icon("quit", color=DANGER_TEXT)
        if not quit_icon.isNull():
            quit_action.setIcon(quit_icon)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        # 左键点击 -> 打开设置窗口
        self._tray.activated.connect(self._on_activated)

    # ==================== 生命周期 ====================

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    # ==================== 公共接口 ====================

    def set_enabled(self, enabled: bool) -> None:
        """同步「启用」菜单勾选状态与图标着色（反映 controller.enabled）。"""
        # 用 blockSignals 避免 setChecked 触发 triggered -> toggle_requested 循环。
        was = self._enable_action.blockSignals(True)
        flag = bool(enabled)
        self._enable_action.setChecked(flag)
        self._sync_enable_action_icon(flag)
        self._enable_action.blockSignals(was)

    def set_quick_lookup_hotkey(self, hotkey_text: str) -> None:
        """同步托盘菜单「快速查词」项上的快捷键提示文案。"""
        hint = _format_hotkey_hint(hotkey_text)
        self._quick_lookup_action.setText(f"快速查词 ({hint})" if hint else "快速查词")

    def show_message(self, title: str, message: str, msecs: int = 3000) -> None:
        """显示托盘气泡通知。"""
        self._tray.showMessage(title, message, QSystemTrayIcon.Information, msecs)

    # ==================== 内部 ====================

    def _sync_enable_action_icon(self, enabled: bool) -> None:
        icon = get_icon("power", color=SUCCESS if enabled else TEXT_MUTED)
        if not icon.isNull():
            self._enable_action.setIcon(icon)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 左键点击（Trigger）打开设置窗口；右键由 Qt 自动弹出 contextMenu。
        if reason == QSystemTrayIcon.Trigger:
            self.settings_requested.emit()

    @classmethod
    def _load_icon(cls) -> QIcon:
        """加载真实应用图标；资源缺失/加载失败时回退 QStyle 占位图标。"""
        icon = load_app_icon()
        if not icon.isNull():
            return icon
        return cls._placeholder_icon()

    @staticmethod
    def _placeholder_icon() -> QIcon:
        """用 QStyle 标准图标占位（SP_MediaPlay）。"""
        style = QApplication.style()
        return style.standardIcon(QStyle.SP_MediaPlay)
