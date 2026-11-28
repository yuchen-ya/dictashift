"""现代 UI 基础设施单测（offscreen）：icons.py、widgets.py、theme.py。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QWidget

from dictashift.ui.icons import ICON_MAP, get_icon, get_pixmap, resolve_icon_name
from dictashift.ui.theme import app_stylesheet, popup_card_stylesheet
from dictashift.ui.widgets import (
    SegmentedControl,
    SettingCard,
    SettingRow,
    SettingSection,
    ToggleSwitch,
    apply_window_chrome,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestIcons:
    def test_semantic_icons_resolve_and_load(self, qapp) -> None:
        for semantic_name in ("general", "hotkeys", "asr", "translation", "personalize", "copy", "close", "search"):
            resolved = resolve_icon_name(semantic_name)
            assert resolved.startswith("ph.")
            icon = get_icon(semantic_name, size=16)
            assert not icon.isNull(), f"icon {semantic_name} ({resolved}) should not be null"

    def test_raw_icon_name_supported(self, qapp) -> None:
        assert resolve_icon_name("mdi6.cog") == "mdi6.cog"
        icon = get_icon("mdi6.cog", color="#ffffff")
        assert not icon.isNull()

    def test_unknown_icon_falls_back_safely(self, qapp) -> None:
        icon = get_icon("totally-nonexistent-icon-xyz")
        assert icon.isNull()
        icon2 = get_icon("ph.nonexistent-glyph-999999")
        assert icon2.isNull()
        pm = get_pixmap("totally-nonexistent-icon-xyz", size=16)
        assert pm.isNull()

    def test_get_pixmap_returns_expected_size(self, qapp) -> None:
        pm = get_pixmap("settings", size=18)
        assert not pm.isNull()
        assert pm.width() == 18
        assert pm.height() == 18

    def test_all_registered_icons_valid(self, qapp) -> None:
        for key in ICON_MAP:
            assert not get_icon(key).isNull(), f"ICON_MAP[{key!r}] failed to load"


class TestWidgets:
    def test_toggle_switch_is_qcheckbox_compatible(self, qapp) -> None:
        sw = ToggleSwitch("测试开关")
        assert isinstance(sw, QCheckBox)
        seen: list[bool] = []
        sw.toggled.connect(seen.append)
        assert sw.isChecked() is False
        sw.setChecked(True)
        assert sw.isChecked() is True
        assert seen == [True]
        sw.setChecked(False)
        assert sw.isChecked() is False
        assert seen == [True, False]
        # 触发一次离屏绘制不抛异常
        pm = sw.grab()
        assert not pm.isNull()

    def test_setting_card_and_section_add_dividers(self, qapp) -> None:
        sec = SettingSection("分组标题")
        r1 = sec.add_row("第一项", ToggleSwitch(), description="说明一")
        r2 = sec.add_row("第二项", QLabel("值"), description="说明二")
        assert isinstance(r1, SettingRow)
        assert isinstance(r2, SettingRow)
        dividers = [w for w in sec.card.findChildren(QWidget) if w.objectName() == "settingDivider"]
        assert len(dividers) == 1
        assert isinstance(sec.card, SettingCard)

    def test_segmented_control_exclusive_and_property(self, qapp) -> None:
        seg = SegmentedControl([("a", "甲"), ("b", "乙"), ("c", "丙")])
        changes: list[str] = []
        seg.selection_changed.connect(changes.append)
        assert seg.current_key() == "a"
        assert seg.button("a").property("segmentBtn") == "selected"

        seg.set_current_key("c")
        assert seg.current_key() == "c"
        assert seg.button("c").property("segmentBtn") == "selected"
        assert seg.button("a").property("segmentBtn") != "selected"
        assert "c" in changes

    def test_apply_window_chrome_safe_offscreen(self, qapp) -> None:
        w = QWidget()
        # offscreen 下不抛异常且优雅降级返回 False
        assert apply_window_chrome(w) is False


class TestTheme:
    def test_app_stylesheet_contains_key_rules(self, qapp) -> None:
        ss = app_stylesheet()
        for token in (
            "QMenu",
            "QMessageBox",
            "QComboBox::down-arrow",
            "QCheckBox::indicator:checked",
            'QPushButton[variant="primary"]',
            'QPushButton[variant="danger"]',
            "QFrame#settingCard",
            "QPushButton#navBtn",
        ):
            assert token in ss

    def test_popup_card_stylesheet_reusable(self, qapp) -> None:
        ss = popup_card_stylesheet("quickLookupCard", text_bg="#111116")
        assert "#quickLookupCard" in ss
        assert "QLineEdit" in ss
        assert "QTextEdit" in ss
