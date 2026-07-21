"""DictaShift UI 层：系统托盘、设置窗口、状态悬浮窗、快捷键编辑控件。"""

from .hotkey_editor import HotkeyEdit, config_to_sequence, sequence_to_config
from .overlay import StateOverlay
from .settings_window import SettingsWindow
from .tray import TrayIcon

__all__ = [
    "HotkeyEdit",
    "config_to_sequence",
    "sequence_to_config",
    "StateOverlay",
    "SettingsWindow",
    "TrayIcon",
]
