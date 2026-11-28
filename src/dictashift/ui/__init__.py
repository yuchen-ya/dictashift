"""DictaShift UI 层：设计系统、轻量组件、图标、托盘、设置窗口与悬浮窗。"""

from .hotkey_editor import HotkeyEdit, config_to_sequence, sequence_to_config
from .icons import get_icon, get_pixmap
from .overlay import StateOverlay
from .quick_lookup_popup import QuickLookupPopup, resolve_lookup_direction
from .result_popup import ResultPopup
from .selection_popup import SelectionPopup
from .settings_window import SettingsWindow
from .theme import app_stylesheet, popup_card_stylesheet
from .tray import TrayIcon
from .widgets import (
    SegmentedControl,
    SettingCard,
    SettingRow,
    SettingSection,
    ToggleSwitch,
    apply_window_chrome,
)

__all__ = [
    "HotkeyEdit",
    "config_to_sequence",
    "sequence_to_config",
    "StateOverlay",
    "QuickLookupPopup",
    "resolve_lookup_direction",
    "ResultPopup",
    "SelectionPopup",
    "SettingsWindow",
    "TrayIcon",
    "ToggleSwitch",
    "SettingCard",
    "SettingRow",
    "SettingSection",
    "SegmentedControl",
    "apply_window_chrome",
    "get_icon",
    "get_pixmap",
    "app_stylesheet",
    "popup_card_stylesheet",
]
