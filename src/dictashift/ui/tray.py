"""系统托盘图标。

行为 (任务规格)：
- QSystemTrayIcon，菜单：「启用 DictaShift」(checkable, 反映 enabled)、
  「设置…」、「退出」。图标用 QStyle 标准图标占位。
- 左键点击托盘图标 = 打开设置窗口。

线程模型：纯 UI 主线程；通过 Qt signal 通知 application 层。
"""

from __future__ import annotations

from PySide6.QtCore import Signal, QObject
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon, QWidget


class TrayIcon(QObject):
    """系统托盘图标 + 菜单。

    信号：
    - toggle_enabled_requested()：「启用」菜单项被点击（application 层据此
      调用 controller.handle_hotkey_press("toggle_enabled") 模拟切换）。
    - settings_requested()：「设置…」或左键点击托盘图标，打开设置窗口。
    - quit_requested()：「退出」。
    """

    toggle_enabled_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(self._placeholder_icon(), self)
        self._tray.setToolTip("DictaShift")

        menu = QMenu()

        self._enable_action = QAction("启用 DictaShift", menu)
        self._enable_action.setCheckable(True)
        self._enable_action.setChecked(True)
        self._enable_action.triggered.connect(self.toggle_enabled_requested.emit)
        menu.addAction(self._enable_action)

        menu.addSeparator()

        settings_action = QAction("设置…", menu)
        settings_action.triggered.connect(self.settings_requested.emit)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("退出", menu)
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
        """同步「启用」菜单勾选状态（反映 controller.enabled）。"""
        # 用 blockSignals 避免setChecked 触发 triggered -> toggle_requested 循环。
        was = self._enable_action.blockSignals(True)
        self._enable_action.setChecked(bool(enabled))
        self._enable_action.blockSignals(was)

    def show_message(self, title: str, message: str, msecs: int = 3000) -> None:
        """显示托盘气泡通知。"""
        self._tray.showMessage(title, message, QSystemTrayIcon.Information, msecs)

    # ==================== 内部 ====================

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 左键点击（Trigger）打开设置窗口；右键由 Qt 自动弹出 contextMenu。
        if reason == QSystemTrayIcon.Trigger:
            self.settings_requested.emit()

    @staticmethod
    def _placeholder_icon() -> QIcon:
        """用 QStyle 标准图标占位（SP_MediaPlay）。"""
        style = QApplication.style()
        return style.standardIcon(QStyle.SP_MediaPlay)
