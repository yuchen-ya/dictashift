"""设置窗口 offscreen 测试。

测试目标：
- ASR 页无推理设备/GPU 控件（GPU 路线已移除）。
- 「当前翻译配置」ComboBox = 内置免登录项 + 用户 LLM profiles（内置在前）。
- 选中内置项：编辑区禁用、「保存为新配置」禁用、提示标签显示；选中用户配置：编辑区启用。
- 保存新配置 backend_type 固定 openai_compatible。
- reset_to_defaults：常规/快捷键/ASR 控件恢复默认，翻译 profiles 不变。
- recheck_hardware_requested 信号无参。
"""

from __future__ import annotations

import os

# 确保在导入 QApplication 之前设置 offscreen 平台，避免真实窗口系统依赖。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dictashift.settings import AppSettings, AsrSettings, HotkeySettings, UiSettings
from dictashift.translation import (
    BUILTIN_BING,
    BUILTIN_GOOGLE,
    TranslationProfile,
)
from dictashift.ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def win(qapp):
    w = SettingsWindow()
    return w


def _combo_items(win: SettingsWindow) -> list[str]:
    return [win._profile_combo.itemText(i) for i in range(win._profile_combo.count())]


# ==================== ASR 页：无 GPU 控件 ====================


class TestAsrPageClean:
    def test_no_device_combo(self, win) -> None:
        assert not hasattr(win, "_device_combo")

    def test_no_gpu_spin(self, win) -> None:
        assert not hasattr(win, "_gpu_spin")

    def test_actual_device_label_kept(self, win) -> None:
        assert hasattr(win, "_actual_device_label")

    def test_recheck_button_kept(self, win) -> None:
        assert hasattr(win, "_recheck_btn")


# ==================== 翻译页：内置项 + 编辑区 ====================


def _load_with_user(win: SettingsWindow) -> None:
    s = AppSettings()
    s.translation.profiles = [
        TranslationProfile(name="myLLM", base_url="http://x/v1", model="m", api_key="k"),
    ]
    s.translation.active_profile = "myLLM"
    win.load_from_settings(s)


class TestTranslationPage:
    def test_combo_builtin_first_then_user(self, win) -> None:
        _load_with_user(win)
        assert _combo_items(win) == [BUILTIN_GOOGLE.name, BUILTIN_BING.name, "myLLM"]

    def test_builtin_selected_disables_edit_and_save(self, win) -> None:
        _load_with_user(win)
        win._profile_combo.setCurrentIndex(0)  # Google
        assert not win._name_edit.isEnabled()
        assert not win._base_url_edit.isEnabled()
        assert not win._save_new_btn.isEnabled()
        # 提示标签显示（not hidden）
        assert not win._builtin_hint_label.isHidden()

    def test_builtin_selected_test_conn_available(self, win) -> None:
        _load_with_user(win)
        win._profile_combo.setCurrentIndex(0)
        assert win._test_conn_btn.isEnabled()

    def test_user_selected_enables_edit_and_save(self, win) -> None:
        _load_with_user(win)
        win._profile_combo.setCurrentText("myLLM")
        assert win._name_edit.isEnabled()
        assert win._base_url_edit.isEnabled()
        assert win._save_new_btn.isEnabled()
        # 提示标签隐藏
        assert win._builtin_hint_label.isHidden()

    def test_save_new_profile_backend_type_openai(self, win) -> None:
        _load_with_user(win)
        win._profile_combo.setCurrentText("myLLM")
        win._name_edit.setText("newprof")
        win._base_url_edit.setText("http://y/v1")
        win._model_edit.setText("mm")
        win._api_key_edit.setText("kk")
        win._save_new_btn.click()
        newp = [p for p in win._profiles if p.name == "newprof"]
        assert len(newp) == 1
        assert newp[0].backend_type == "openai_compatible"

    def test_save_new_missing_name_shows_error(self, win) -> None:
        _load_with_user(win)
        win._profile_combo.setCurrentText("myLLM")
        win._name_edit.clear()
        win._save_new_btn.click()
        assert not any(p.name == "" for p in win._profiles)
        assert "配置名称" in win._test_result_label.text()

    def test_builtin_active_survives_apply(self, win) -> None:
        s = AppSettings()
        s.translation.active_profile = BUILTIN_GOOGLE.name
        win.load_from_settings(s)
        out = win.apply_to_settings(s)
        assert out.translation.active_profile == BUILTIN_GOOGLE.name
        # 内置项不进入持久化 profiles
        assert out.translation.profiles == []

    def test_user_active_survives_apply(self, win) -> None:
        _load_with_user(win)
        out = win.apply_to_settings(AppSettings())
        assert out.translation.active_profile == "myLLM"
        names = [p.name for p in out.translation.profiles]
        assert "myLLM" in names


# ==================== reset_to_defaults ====================


class TestResetToDefaults:
    def test_hotkeys_reset_to_defaults(self, win) -> None:
        # 先改成非默认值
        s = AppSettings()
        s.hotkeys = HotkeySettings(dictate_zh="f1", dictate_en="f2", cancel="x")
        s.translation.profiles = [
            TranslationProfile(name="myLLM", base_url="http://x/v1", model="m"),
        ]
        s.translation.active_profile = "myLLM"
        win.load_from_settings(s)
        # 重置
        win.reset_to_defaults()
        assert win._dictate_zh_edit.config_text() == "f8"
        assert win._dictate_en_edit.config_text() == "f9"
        assert win._cancel_edit.config_text() == "esc"

    def test_general_and_ui_reset(self, win) -> None:
        s = AppSettings()
        s.enabled = False
        s.ui = UiSettings(result_ui="overlay", launch_at_login=True)
        win.load_from_settings(s)
        win.reset_to_defaults()
        # enabled 默认 True，result_ui 默认 popup
        assert win._enable_check.isChecked() is True
        assert win._current_result_ui() == "popup"

    def test_asr_reset_to_defaults(self, win) -> None:
        s = AppSettings()
        s.asr = AsrSettings(max_record_seconds=20, mic_device_index=None)
        win.load_from_settings(s)
        win.reset_to_defaults()
        assert win._max_record_spin.value() == 60  # AsrSettings 默认

    def test_translation_profiles_untouched(self, win) -> None:
        s = AppSettings()
        s.translation.profiles = [
            TranslationProfile(name="myLLM", base_url="http://x/v1", model="m"),
            TranslationProfile(name="other", base_url="http://z/v1", model="o"),
        ]
        s.translation.active_profile = "myLLM"
        win.load_from_settings(s)
        win.reset_to_defaults()
        assert [p.name for p in win._profiles] == ["myLLM", "other"]

    def test_reset_shows_status_label(self, win) -> None:
        win.reset_to_defaults()
        assert win._reset_status_label.text()  # 非空提示


# ==================== 常规页：结果展示方式 ComboBox ====================


class TestResultUiCombo:
    def test_combo_has_two_items_with_data(self, win) -> None:
        items = {
            win._result_ui_combo.itemData(i)
            for i in range(win._result_ui_combo.count())
        }
        assert items == {"popup", "overlay"}
        assert win._result_ui_combo.itemData(0) == "popup"  # 默认项在前

    def test_no_legacy_overlay_check(self, win) -> None:
        # 旧 CheckBox 控件已移除
        assert not hasattr(win, "_overlay_check")

    def test_load_popup(self, win) -> None:
        s = AppSettings()
        s.ui = UiSettings(result_ui="popup")
        win.load_from_settings(s)
        assert win._current_result_ui() == "popup"

    def test_load_overlay(self, win) -> None:
        s = AppSettings()
        s.ui = UiSettings(result_ui="overlay")
        win.load_from_settings(s)
        assert win._current_result_ui() == "overlay"

    def test_apply_writes_result_ui(self, win) -> None:
        s = AppSettings()
        s.ui = UiSettings(result_ui="popup")
        win.load_from_settings(s)
        # 切换到 overlay
        idx = win._result_ui_combo.findData("overlay")
        win._result_ui_combo.setCurrentIndex(idx)
        out = win.apply_to_settings(s)
        assert out.ui.result_ui == "overlay"

    def test_reset_to_defaults_sets_popup(self, win) -> None:
        s = AppSettings()
        s.ui = UiSettings(result_ui="overlay")
        win.load_from_settings(s)
        win.reset_to_defaults()
        assert win._current_result_ui() == UiSettings().result_ui == "popup"


# ==================== recheck 信号无参 ====================


def test_recheck_signal_parameterless(win) -> None:
    got = {}
    win.recheck_hardware_requested.connect(lambda: got.__setitem__("fired", True))
    win._recheck_btn.click()
    assert got.get("fired") is True


def test_recheck_sets_loading_state(win) -> None:
    win._recheck_btn.click()
    assert not win._recheck_btn.isEnabled()
    assert win._recheck_btn.text() == "加载中…"
