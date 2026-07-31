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

from dictashift.settings import (
    AppSettings,
    AsrSettings,
    HotkeySettings,
    PersonalizeSettings,
    UiSettings,
)
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


# ==================== 个性化页 ====================


class TestPersonalizeTab:
    def test_tab_added_as_fifth_page(self, win) -> None:
        assert win._tabs.count() == 5
        assert win._tabs.tabText(4) == "个性化"

    def test_suffix_edit_follows_checkbox(self, win) -> None:
        win._suffix_check.setChecked(False)
        assert not win._suffix_edit.isEnabled()
        win._suffix_check.setChecked(True)
        assert win._suffix_edit.isEnabled()

    def test_load_from_settings(self, win) -> None:
        s = AppSettings()
        s.personalize = PersonalizeSettings(
            suffix_enabled=True, suffix_text="捏", rewrite_style="toxic"
        )
        win.load_from_settings(s)
        assert win._suffix_check.isChecked() is True
        assert win._suffix_edit.text() == "捏"
        assert win._suffix_edit.isEnabled()
        assert win._rewrite_toxic_radio.isChecked()
        assert not win._rewrite_off_radio.isChecked()
        assert not win._rewrite_tsundere_radio.isChecked()

    def test_load_defaults(self, win) -> None:
        win.load_from_settings(AppSettings())
        assert win._suffix_check.isChecked() is False
        assert win._suffix_edit.text() == "喵"
        assert not win._suffix_edit.isEnabled()
        assert win._rewrite_off_radio.isChecked()

    def test_radio_mutually_exclusive(self, win) -> None:
        win._rewrite_tsundere_radio.setChecked(True)
        assert win._current_rewrite_style() == "tsundere"
        win._rewrite_toxic_radio.setChecked(True)
        assert not win._rewrite_tsundere_radio.isChecked()
        assert win._current_rewrite_style() == "toxic"

    def test_apply_writes_personalize(self, win) -> None:
        win.load_from_settings(AppSettings())
        win._suffix_check.setChecked(True)
        win._suffix_edit.setText("哦")
        win._rewrite_tsundere_radio.setChecked(True)
        out = win.apply_to_settings(AppSettings())
        assert out.personalize.suffix_enabled is True
        assert out.personalize.suffix_text == "哦"
        assert out.personalize.rewrite_style == "tsundere"

    def test_load_apply_round_trip(self, win) -> None:
        s = AppSettings()
        s.personalize = PersonalizeSettings(
            suffix_enabled=True, suffix_text="捏", rewrite_style="toxic"
        )
        win.load_from_settings(s)
        out = win.apply_to_settings(s)
        assert out.personalize == s.personalize

    def test_reset_to_defaults_resets_personalize(self, win) -> None:
        s = AppSettings()
        s.personalize = PersonalizeSettings(
            suffix_enabled=True, suffix_text="捏", rewrite_style="tsundere"
        )
        win.load_from_settings(s)
        win.reset_to_defaults()
        assert win._suffix_check.isChecked() is False
        assert win._suffix_edit.text() == PersonalizeSettings().suffix_text
        assert win._rewrite_off_radio.isChecked()


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


# ==================== 测试右下角弹窗按钮 ====================


def test_test_popup_button_emits_signal(win) -> None:
    """「测试右下角弹窗」按钮点击应 emit test_popup_requested（无参）。"""
    got = {}
    win.test_popup_requested.connect(lambda: got.__setitem__("fired", True))
    win._test_popup_btn.click()
    assert got.get("fired") is True


def test_test_popup_button_exists(win) -> None:
    assert hasattr(win, "_test_popup_btn")
    assert win._test_popup_btn.text() == "测试右下角弹窗"


# ==================== 模型状态 / 下载 UI ====================


class TestModelDownloadUi:
    def test_model_status_label_and_download_button_exist(self, win) -> None:
        assert hasattr(win, "_model_status_label")
        assert hasattr(win, "_download_model_btn")
        assert hasattr(win, "_model_progress")

    def test_progress_bar_hidden_by_default(self, win) -> None:
        assert win._model_progress.isHidden()

    def test_refresh_model_status_shows_installed_when_ready(self, win) -> None:
        # 仓库 assets 齐全（开发环境），应为"已安装"。
        win.refresh_model_status()
        assert "已安装" in win._model_status_label.text()
        assert win._download_model_btn.isHidden()

    def test_refresh_model_status_shows_not_installed_when_missing(
        self, win, monkeypatch
    ) -> None:
        from dictashift.asr import providers as prov

        monkeypatch.setattr(prov, "models_ready", lambda d: False)
        win.refresh_model_status()
        assert win._model_status_label.text() == "未安装"
        assert not win._download_model_btn.isHidden()

    def test_download_button_emits_signal(self, win) -> None:
        got = {}
        win.model_download_requested.connect(lambda: got.__setitem__("fired", True))
        win._download_model_btn.show()  # 确保可见
        win._download_model_btn.click()
        assert got.get("fired") is True
        # 点击后按钮禁用、进度条显示。
        assert not win._download_model_btn.isEnabled()
        assert not win._model_progress.isHidden()

    def test_set_progress_percent(self, win) -> None:
        win.set_model_download_progress(42)
        assert win._model_progress.value() == 42
        assert not win._download_model_btn.isEnabled()

    def test_set_progress_busy_when_unknown(self, win) -> None:
        win.set_model_download_progress(-1)
        # 忙碌态：min==max==0
        assert win._model_progress.minimum() == 0
        assert win._model_progress.maximum() == 0

    def test_on_complete_restores_and_refreshes(self, win) -> None:
        win.set_model_download_progress(50)
        win.on_model_download_complete()
        assert win._model_progress.isHidden()
        assert win._download_model_btn.isEnabled()
        assert win._download_model_btn.text() == "下载模型"
        # 状态刷新为已安装（开发环境 assets 齐全）。
        assert "已安装" in win._model_status_label.text()

    def test_on_failed_shows_error_and_restores_button(self, win, monkeypatch) -> None:
        from dictashift.asr import providers as prov

        monkeypatch.setattr(prov, "models_ready", lambda d: False)
        win.on_model_download_failed("网络错误")
        assert win._model_progress.isHidden()
        assert win._download_model_btn.isEnabled()
        assert "网络错误" in win._model_status_label.text()


# ==================== 开机启动：勾选状态来自注册表 ====================


class TestLaunchAtLoginCheckbox:
    def test_load_reads_registry_not_settings(self, win, monkeypatch) -> None:
        """load_from_settings 时勾选状态来自 is_launch_at_login_enabled。"""
        from dictashift.ui import settings_window as sw_mod

        monkeypatch.setattr(sw_mod, "is_launch_at_login_enabled", lambda: True)
        s = AppSettings()
        s.ui = UiSettings(launch_at_login=False)  # settings 值与注册表相反
        win.load_from_settings(s)
        assert win._autostart_check.isChecked() is True

    def test_apply_writes_registry(self, win, monkeypatch) -> None:
        from dictashift.ui import settings_window as sw_mod

        calls = {}

        def _set(enabled: bool) -> None:
            calls["enabled"] = enabled

        monkeypatch.setattr(sw_mod, "set_launch_at_login", _set)
        monkeypatch.setattr(sw_mod, "is_launch_at_login_enabled", lambda: True)
        win._autostart_check.setChecked(True)
        win.apply_to_settings(AppSettings())
        assert calls.get("enabled") is True

    def test_apply_registry_failure_warns_and_rolls_back(
        self, win, monkeypatch
    ) -> None:
        """注册表写入失败 -> 不抛、回滚复选框为注册表实际值。"""
        from dictashift.ui import settings_window as sw_mod

        def _raise(enabled: bool) -> None:
            raise OSError("denied")

        # 禁用 QMessageBox 的真实弹窗。
        monkeypatch.setattr(
            "PySide6.QtWidgets.QMessageBox.warning",
            lambda *a, **kw: 0,
        )
        monkeypatch.setattr(sw_mod, "set_launch_at_login", _raise)
        monkeypatch.setattr(sw_mod, "is_launch_at_login_enabled", lambda: False)
        win._autostart_check.setChecked(True)
        # 不应抛
        win.apply_to_settings(AppSettings())
        # 回滚为注册表实际值 False
        assert win._autostart_check.isChecked() is False


def test_autostart_checkbox_label_has_no_future_version_note(win) -> None:
    """「将在后续版本生效」标注已被移除。"""
    text = win._autostart_check.text()
    assert "后续版本" not in text
    assert "生效" not in text


# ==================== 清除全部数据并退出 按钮 ====================


class TestClearDataButton:
    def test_button_exists_with_danger_style(self, win) -> None:
        """按钮存在、文案正确、含危险（红色）样式。"""
        assert hasattr(win, "_clear_data_btn")
        btn = win._clear_data_btn
        assert btn.text() == "清除全部数据并退出…"
        style = btn.styleSheet()
        assert "#c0392b" in style or "#ff6b6b" in style  # 红色边框/文字

    def test_cancel_does_not_emit(self, win, monkeypatch) -> None:
        """确认框返回 Cancel -> 不 emit clear_data_requested。"""
        from PySide6.QtWidgets import QMessageBox

        from dictashift.ui import settings_window as sw_mod

        monkeypatch.setattr(
            sw_mod.QMessageBox,
            "warning",
            lambda *a, **kw: QMessageBox.StandardButton.Cancel,
        )
        got = {}
        win.clear_data_requested.connect(lambda: got.__setitem__("fired", True))
        win._clear_data_btn.click()
        assert got.get("fired") is None

    def test_confirm_emits_once(self, win, monkeypatch) -> None:
        """确认框返回 Yes -> emit clear_data_requested 一次。"""
        from PySide6.QtWidgets import QMessageBox

        from dictashift.ui import settings_window as sw_mod

        monkeypatch.setattr(
            sw_mod.QMessageBox,
            "warning",
            lambda *a, **kw: QMessageBox.StandardButton.Yes,
        )
        got = []
        win.clear_data_requested.connect(lambda: got.append(1))
        win._clear_data_btn.click()
        assert got == [1]
