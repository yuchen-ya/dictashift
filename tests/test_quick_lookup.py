"""快速查词功能全面测试（配置/热键/设置页/托盘/方向检测/极简两段式浮窗/应用智能联动）。

覆盖范围：
- HotkeySettings.quick_lookup 默认值 "alt+q"、round-trip 与旧配置缺键回退。
- HotkeyService：quick_lookup 属于 press-only、alt+q 触发一次、松开不触发 release、热更新生效。
- SettingsWindow：快速查词热键加载、保存（apply_to_settings）与恢复默认（reset_to_defaults）。
- TrayIcon：快速查词菜单项显示当前热键、支持热键更新同步文案、点击发射 quick_lookup_requested。
- resolve_lookup_direction：纯中文、纯英文、中英混合、数字/标点、手动 zh2en/en2zh。
- QuickLookupPopup（极简两段式 Spotlight，offscreen）：
  * 无填入光标/图钉/关闭按钮/分段栏等冗余元素；
  * 空闲态单行收起，清空按钮仅非空显示；
  * 方向循环胶囊按钮点击循环 auto → zh2en → en2zh → auto 且非空立即重译；
  * 预填打开立即翻译、防抖自动翻译、Enter 取消防抖立即翻译、清空取消防抖并收起结果行；
  * 请求序号防乱序覆盖（过期结果与过期错误均丢弃）；
  * 结果态显示「复制」按钮（写入剪贴板 + 瞬态「已复制」反馈、Ctrl+Shift+C）；
  * 错误态切换为「重试」按钮，点击重新触发翻译；加载态隐藏动作按钮；
  * Esc 隐藏、失焦自动隐藏（含 250ms 唤起宽限期）、居中定位与屏幕边界夹取。
- DictaShiftApplication 智能联动：
  * 热键 press/release 路由与 _bindings_from_settings；
  * 修饰键释放等待（_wait_modifiers_released）与等待期间捕获序号过时跳过取词；
  * 有选区预填打开、无选区空白打开、并发防重入、可见时再按热键 toggle 关闭；
  * 托盘菜单直接打开空白浮窗（不触发选区抓取）；
  * 浮窗可见时抑制全局 cancel(Esc) 穿透，隐藏后恢复正常；
  * 翻译复用最新配置、未配置时无缝回退 BUILTIN_GOOGLE、并发上限 2 与取消同步。
"""

from __future__ import annotations

import os
import queue
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import dictashift.application as appmod
import dictashift.ui.quick_lookup_popup as qlmod
from dictashift.application import DictaShiftApplication
from dictashift.hotkeys.matcher import HotkeyBinding, parse_hotkey
from dictashift.hotkeys.service import HotkeyService, _PRESS_ONLY_ACTIONS
from dictashift.selection.capture import CaptureResult, CaptureStatus
from dictashift.settings.models import AppSettings, HotkeySettings
from dictashift.translation import BUILTIN_GOOGLE, TranslationError, TranslationProfile
from dictashift.ui.quick_lookup_popup import QuickLookupPopup, resolve_lookup_direction
from dictashift.ui.settings_window import SettingsWindow
from dictashift.ui.tray import TrayIcon


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ==================== 1. 配置模型与向后兼容 ====================


class TestHotkeySettingsQuickLookup:
    def test_default_value_is_alt_q(self) -> None:
        hk = HotkeySettings()
        assert hk.quick_lookup == "alt+q"
        assert AppSettings().hotkeys.quick_lookup == "alt+q"

    def test_to_dict_and_from_dict_round_trip(self) -> None:
        hk = HotkeySettings(quick_lookup="ctrl+shift+q")
        d = hk.to_dict()
        assert d["quick_lookup"] == "ctrl+shift+q"
        restored = HotkeySettings.from_dict(d)
        assert restored.quick_lookup == "ctrl+shift+q"

    def test_legacy_settings_missing_quick_lookup_falls_back(self) -> None:
        legacy = {
            "dictate_zh": "f8",
            "dictate_en": "f9",
            "toggle_enabled": "ctrl+alt+f12",
            "cancel": "esc",
            "selection_translate": "f7",
        }
        hk = HotkeySettings.from_dict(legacy)
        assert hk.quick_lookup == "alt+q"
        app_settings = AppSettings.from_dict({"hotkeys": legacy})
        assert app_settings.hotkeys.quick_lookup == "alt+q"


# ==================== 2. HotkeyService 单次触发与热更新 ====================


class TestHotkeyServiceQuickLookup:
    def test_quick_lookup_is_press_only(self) -> None:
        assert "quick_lookup" in _PRESS_ONLY_ACTIONS
        presses: list[str] = []
        releases: list[str] = []
        svc = HotkeyService(
            [HotkeyBinding(action="quick_lookup", keys=parse_hotkey("alt+q"))],
            on_press=presses.append,
            on_release=releases.append,
        )
        svc._handle_press_locked("alt")
        svc._handle_press_locked("q")
        svc._handle_press_locked("q")
        svc._handle_release_locked("q")
        svc._handle_release_locked("alt")
        assert presses == ["quick_lookup"]
        assert releases == []

    def test_quick_lookup_update_bindings(self) -> None:
        presses: list[str] = []
        svc = HotkeyService(
            [HotkeyBinding(action="quick_lookup", keys=parse_hotkey("alt+q"))],
            on_press=presses.append,
            on_release=lambda _a: None,
        )
        svc.update_bindings(
            [HotkeyBinding(action="quick_lookup", keys=parse_hotkey("ctrl+shift+k"))]
        )
        svc._handle_press_locked("alt")
        svc._handle_press_locked("q")
        svc._handle_release_locked("q")
        svc._handle_release_locked("alt")
        assert presses == []

        svc._handle_press_locked("ctrl")
        svc._handle_press_locked("shift")
        svc._handle_press_locked("k")
        assert presses == ["quick_lookup"]


# ==================== 3. 设置窗口与托盘菜单 ====================


class TestSettingsWindowAndTrayQuickLookup:
    def test_settings_window_load_apply_reset_quick_lookup(self, qapp) -> None:
        win = SettingsWindow()
        s = AppSettings(hotkeys=HotkeySettings(quick_lookup="ctrl+alt+d"))
        win.load_from_settings(s)
        assert win._quick_lookup_edit.config_text() == "ctrl+alt+d"

        win._quick_lookup_edit.set_config_text("ctrl+shift+l")
        updated = win.apply_to_settings(s)
        assert updated.hotkeys.quick_lookup == "ctrl+shift+l"

        win.reset_to_defaults()
        assert win._quick_lookup_edit.config_text() == "alt+q"

    def test_tray_quick_lookup_action_and_hotkey_sync(self, qapp) -> None:
        tray = TrayIcon()
        assert "快速查词" in tray._quick_lookup_action.text()
        assert "Alt+Q" in tray._quick_lookup_action.text()

        tray.set_quick_lookup_hotkey("ctrl+shift+k")
        assert tray._quick_lookup_action.text() == "快速查词 (Ctrl+Shift+K)"

        tray.set_quick_lookup_hotkey("")
        assert tray._quick_lookup_action.text() == "快速查词"

        triggered: list[bool] = []
        tray.quick_lookup_requested.connect(lambda: triggered.append(True))
        tray._quick_lookup_action.trigger()
        assert triggered == [True]


# ==================== 4. 方向解析纯函数 ====================


class TestResolveLookupDirection:
    @pytest.mark.parametrize(
        ("text", "mode", "expected"),
        [
            ("分布式系统", "auto", "zh2en"),
            ("你好世界！", "auto", "zh2en"),
            ("ephemeral", "auto", "en2zh"),
            ("Eventual consistency in distributed systems.", "auto", "en2zh"),
            ("API v2.0 rate-limit", "auto", "en2zh"),
            ("如何在 Python 中使用 asyncio", "auto", "zh2en"),
            ("中文 API", "auto", "zh2en"),
            ("123456", "auto", "zh2en"),
            ("!!! ???", "auto", "zh2en"),
            ("hello world", "zh2en", "zh2en"),
            ("你好世界", "en2zh", "en2zh"),
        ],
    )
    def test_resolve_direction_cases(self, text: str, mode: str, expected: str) -> None:
        assert resolve_lookup_direction(text, mode) == expected


# ==================== 5. QuickLookupPopup 极简两段式 UI 与交互 ====================


class TestQuickLookupPopup:
    def test_minimalist_structure_has_no_insert_pin_close_or_segmented(self, qapp) -> None:
        popup = QuickLookupPopup()
        assert popup.width() == 520
        assert not hasattr(popup, "insert_requested")
        assert not hasattr(popup, "_insert_btn")
        assert not hasattr(popup, "_pin_btn")
        assert not hasattr(popup, "_close_btn")
        assert not hasattr(popup, "_mode_segment")
        assert not hasattr(popup, "_action_bar")
        assert hasattr(qlmod, "_focus_window_windows")
        assert not hasattr(appmod, "_get_foreground_window")
        assert not hasattr(appmod, "_restore_foreground_window")
        assert not hasattr(DictaShiftApplication, "_on_quick_lookup_insert")
        popup.close()

    def test_open_blank_is_single_tier_bar(self, qapp) -> None:
        popup = QuickLookupPopup()
        reqs: list[tuple[int, str, str]] = []
        popup.translate_requested.connect(lambda rid, t, d: reqs.append((rid, t, d)))

        popup.open_lookup(None)
        assert popup.isVisible()
        assert popup._input.text() == ""
        assert popup._clear_btn.isHidden()
        assert popup._result_divider.isHidden()
        assert popup._result_container.isHidden()
        assert reqs == []
        popup.close()

    def test_open_with_prefill_triggers_translate_and_hides_buttons_while_loading(self, qapp) -> None:
        popup = QuickLookupPopup()
        reqs: list[tuple[int, str, str]] = []
        popup.translate_requested.connect(lambda rid, t, d: reqs.append((rid, t, d)))

        popup.open_lookup("  distributed cache  ")
        assert popup._input.text() == "distributed cache"
        assert not popup._clear_btn.isHidden()
        assert len(reqs) == 1
        req_id, text, direction = reqs[0]
        assert req_id == popup.request_seq
        assert text == "distributed cache"
        assert direction == "en2zh"
        assert not popup._result_divider.isHidden()
        assert not popup._result_container.isHidden()
        assert not popup._loading_label.isHidden()
        assert popup._copy_btn.isHidden()
        assert popup._retry_btn.isHidden()
        popup.close()

    def test_debounce_triggers_translation_and_enter_triggers_immediately(self, qapp) -> None:
        popup = QuickLookupPopup()
        popup._debounce_timer.setInterval(40)
        reqs: list[tuple[int, str, str]] = []
        popup.translate_requested.connect(lambda rid, t, d: reqs.append((rid, t, d)))

        popup.open_lookup(None)
        popup._input.setText("幂等性")
        assert not popup._clear_btn.isHidden()
        assert reqs == []
        QTest.qWait(90)
        assert len(reqs) == 1
        assert reqs[0][1:] == ("幂等性", "zh2en")

        popup._input.setText("latency")
        assert popup._debounce_timer.isActive()
        popup._on_return_pressed()
        assert not popup._debounce_timer.isActive()
        assert len(reqs) == 2
        assert reqs[1][1:] == ("latency", "en2zh")
        QTest.qWait(80)
        assert len(reqs) == 2
        popup.close()

    def test_clear_input_cancels_debounce_emits_cancel_and_collapses(self, qapp) -> None:
        popup = QuickLookupPopup()
        cancelled: list[int] = []
        popup.request_cancelled.connect(cancelled.append)

        popup.open_lookup("hello")
        seq_before = popup.request_seq
        popup.show_result(seq_before, "你好", "en2zh")
        assert not popup._result_container.isHidden()

        popup._input.setText("world")
        assert popup._debounce_timer.isActive()
        popup._clear_btn.click()
        assert not popup._debounce_timer.isActive()
        assert popup._clear_btn.isHidden()
        assert popup.request_seq > seq_before
        assert cancelled[-1] == popup.request_seq
        assert popup._result_divider.isHidden()
        assert popup._result_container.isHidden()
        assert popup.current_result == ""
        popup.close()

    def test_direction_pill_cycles_modes_and_retranslates(self, qapp) -> None:
        popup = QuickLookupPopup()
        reqs: list[tuple[int, str, str]] = []
        popup.translate_requested.connect(lambda rid, t, d: reqs.append((rid, t, d)))

        assert popup.direction_mode() == "auto"
        assert popup._dir_btn.text() == "自动"
        assert popup._dir_btn.property("locked") == "false"

        popup.open_lookup("hello")
        assert reqs[-1][2] == "en2zh"

        # 点击循环：auto -> zh2en
        popup._dir_btn.click()
        assert popup.direction_mode() == "zh2en"
        assert popup._dir_btn.text() == "中→英"
        assert popup._dir_btn.property("locked") == "true"
        assert len(reqs) == 2
        assert reqs[-1][1:] == ("hello", "zh2en")

        # 点击循环：zh2en -> en2zh
        popup._dir_btn.click()
        assert popup.direction_mode() == "en2zh"
        assert popup._dir_btn.text() == "英→中"
        assert popup._dir_btn.property("locked") == "true"
        assert len(reqs) == 3
        assert reqs[-1][1:] == ("hello", "en2zh")

        # 点击循环：en2zh -> auto
        popup._dir_btn.click()
        assert popup.direction_mode() == "auto"
        assert popup._dir_btn.text() == "自动"
        assert popup._dir_btn.property("locked") == "false"
        assert len(reqs) == 4
        assert reqs[-1][1:] == ("hello", "en2zh")
        popup.close()

    def test_request_seq_drops_stale_result_and_stale_error(self, qapp) -> None:
        popup = QuickLookupPopup()
        popup.open_lookup("first")
        seq1 = popup.request_seq

        popup._input.setText("second")
        popup._on_return_pressed()
        seq2 = popup.request_seq
        assert seq2 > seq1

        popup.show_result(seq1, "过期译文", "en2zh")
        assert popup.current_result == ""
        assert popup._result_edit.isHidden()

        popup.show_error(seq1, "过期错误")
        assert popup._error_label.isHidden()

        popup.show_result(seq2, "最新译文", "en2zh")
        assert popup.current_result == "最新译文"
        assert popup._result_edit.toPlainText() == "最新译文"
        assert not popup._result_edit.isHidden()
        assert not popup._copy_btn.isHidden()
        assert popup._retry_btn.isHidden()
        popup.close()

    def test_copy_button_shortcut_and_error_retry_swap(self, qapp, monkeypatch) -> None:
        copied: list[str] = []
        monkeypatch.setattr(qlmod, "set_clipboard_text", copied.append)

        popup = QuickLookupPopup()
        reqs: list[tuple[int, str, str]] = []
        popup.translate_requested.connect(lambda rid, t, d: reqs.append((rid, t, d)))

        popup.open_lookup("你好")
        popup.show_result(popup.request_seq, "Hello", "zh2en")
        assert not popup._copy_btn.isHidden()
        assert popup._retry_btn.isHidden()

        # 点击复制 -> 写剪贴板 + 「已复制」瞬态反馈
        popup._copy_btn.click()
        assert copied == ["Hello"]
        assert popup._copy_btn.text() == "已复制"

        # 快捷键 Ctrl+Shift+C
        ev_copy = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        popup.keyPressEvent(ev_copy)
        assert copied == ["Hello", "Hello"]

        # 错误态：隐藏复制按钮，切换为「重试」按钮，点击后重新发起翻译
        popup.show_error(popup.request_seq, "网络超时")
        assert popup._copy_btn.isHidden()
        assert not popup._retry_btn.isHidden()
        assert popup._error_label.text() == "网络超时"

        before_retry = len(reqs)
        popup._retry_btn.click()
        assert len(reqs) == before_retry + 1
        assert reqs[-1][1:] == ("你好", "zh2en")
        popup.close()

    def test_esc_hides_popup_and_accepts_event(self, qapp) -> None:
        popup = QuickLookupPopup()
        popup.open_lookup("test")
        assert popup.isVisible()

        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        popup.keyPressEvent(ev)
        assert ev.isAccepted()
        assert not popup.isVisible()
        popup.close()

    def test_deactivate_auto_closes_after_grace_period(self, qapp, monkeypatch) -> None:
        now = [100.0]
        monkeypatch.setattr(qlmod.time, "monotonic", lambda: now[0])

        popup = QuickLookupPopup()
        popup.open_lookup("test")
        assert popup.isVisible()

        # 250ms 宽限期内失焦不隐藏
        now[0] = 100.10
        popup.changeEvent(QEvent(QEvent.Type.WindowDeactivate))
        assert popup.isVisible()

        # 超过宽限期 -> 失焦自动关闭
        now[0] = 100.50
        popup.changeEvent(QEvent(QEvent.Type.WindowDeactivate))
        assert not popup.isVisible()
        popup.close()

    def test_centered_positioning_clamps_to_screen(self, qapp) -> None:
        popup = QuickLookupPopup()
        popup.open_lookup(None)
        screen = popup._target_screen()
        if screen is not None:
            geo = screen.availableGeometry()
            assert geo.left() <= popup.x() <= geo.right()
            assert geo.top() <= popup.y() <= geo.bottom()
        popup.close()


# ==================== 6. DictaShiftApplication 智能联动 ====================


class _FakeSignal:
    def __init__(self) -> None:
        self.emitted: list[tuple] = []
        self.event = threading.Event()

    def emit(self, *args) -> None:
        self.emitted.append(args)
        self.event.set()


class _FakeBridge:
    def __init__(self) -> None:
        self.selection_toggle = _FakeSignal()
        self.quick_lookup_trigger = _FakeSignal()
        self.quick_lookup_captured = _FakeSignal()
        self.quick_lookup_result = _FakeSignal()
        self.quick_lookup_error = _FakeSignal()


class _FakeQuickLookupPopup:
    def __init__(self) -> None:
        self.visible = False
        self.opened_with: list[str | None] = []
        self.results: list[tuple[int, str, str]] = []
        self.errors: list[tuple[int, str]] = []
        self.hide_calls = 0
        self.close_calls = 0

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def open_lookup(self, initial_text: str | None = None) -> None:
        self.visible = True
        self.opened_with.append(initial_text)

    def hide(self) -> None:
        self.visible = False
        self.hide_calls += 1

    def close(self) -> None:
        self.visible = False
        self.close_calls += 1

    def show_result(self, request_id: int, translated: str, direction: str = "") -> None:
        self.results.append((request_id, translated, direction))

    def show_error(self, request_id: int, message: str) -> None:
        self.errors.append((request_id, message))


class _FakeController:
    def __init__(self, profile: TranslationProfile | None) -> None:
        self._profile = profile

    def get_active_profile(self) -> TranslationProfile | None:
        return self._profile


class _FakeBackend:
    def __init__(self, result: str = "translated text", error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, TranslationProfile]] = []

    def translate(self, text: str, profile: TranslationProfile) -> str:
        self.calls.append((text, profile))
        if self._error is not None:
            raise self._error
        return self._result


def _make_ql_app(
    *,
    profile: TranslationProfile | None = None,
    backend=None,
) -> DictaShiftApplication:
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._bridge = _FakeBridge()
    app._quick_lookup_popup = _FakeQuickLookupPopup()
    app._quick_lookup_lock = threading.Lock()
    app._quick_lookup_visible = False
    app._quick_lookup_capturing = False
    app._quick_lookup_capture_seq = 0
    app._quick_lookup_req_seq = 0
    app._quick_lookup_workers = 0
    app._quick_lookup_pending = None
    app._controller = _FakeController(profile)
    app._backend = backend if backend is not None else _FakeBackend()
    app._hotkey_queue = queue.Queue()
    app.settings = AppSettings()
    return app


class TestApplicationQuickLookup:
    def test_hotkey_routing_and_bindings(self) -> None:
        app = _make_ql_app()
        app._on_hotkey_press("quick_lookup")
        assert app._bridge.quick_lookup_trigger.emitted == [()]
        assert app._hotkey_queue.empty()

        app._on_hotkey_release("quick_lookup")
        assert app._hotkey_queue.empty()

        bindings = DictaShiftApplication._bindings_from_settings(AppSettings())
        by_action = {b.action: b.keys for b in bindings}
        assert by_action["quick_lookup"] == frozenset({"alt", "q"})

    def test_hotkey_with_selection_waits_modifiers_then_prefills(self, monkeypatch) -> None:
        app = _make_ql_app()
        order: list[str] = []
        monkeypatch.setattr(
            appmod,
            "_wait_modifiers_released",
            lambda *a, **kw: (order.append("wait_mods"), True)[1],
        )

        def _fake_capture(**kw):
            order.append("capture")
            return CaptureResult(CaptureStatus.SUCCESS, text="  idempotent  ", length=14)

        monkeypatch.setattr(appmod, "capture_selection", _fake_capture)

        app._on_quick_lookup_hotkey()
        assert app._bridge.quick_lookup_captured.event.wait(timeout=3.0)
        assert order == ["wait_mods", "capture"]
        token, text = app._bridge.quick_lookup_captured.emitted[0]
        assert text == "idempotent"

        app._on_quick_lookup_captured(token, text)
        assert app._quick_lookup_popup.opened_with == ["idempotent"]

    def test_hotkey_without_selection_opens_blank_popup(self, monkeypatch) -> None:
        app = _make_ql_app()
        monkeypatch.setattr(appmod, "_wait_modifiers_released", lambda *a, **kw: True)
        monkeypatch.setattr(
            appmod,
            "capture_selection",
            lambda **kw: CaptureResult(CaptureStatus.NO_TEXT, reason="无选区"),
        )

        app._on_quick_lookup_hotkey()
        assert app._bridge.quick_lookup_captured.event.wait(timeout=3.0)
        token, text = app._bridge.quick_lookup_captured.emitted[0]
        assert text == ""

        app._on_quick_lookup_captured(token, text)
        assert app._quick_lookup_popup.opened_with == [None]

    def test_stale_capture_seq_during_modifier_wait_skips_capture(self, monkeypatch) -> None:
        app = _make_ql_app()
        wait_entered = threading.Event()
        wait_release = threading.Event()
        capture_called: list[bool] = []

        def _blocking_wait(*a, **kw) -> bool:
            wait_entered.set()
            wait_release.wait(timeout=3.0)
            return True

        monkeypatch.setattr(appmod, "_wait_modifiers_released", _blocking_wait)
        monkeypatch.setattr(
            appmod,
            "capture_selection",
            lambda **kw: (capture_called.append(True), CaptureResult(CaptureStatus.NO_TEXT))[1],
        )

        app._on_quick_lookup_hotkey()
        assert wait_entered.wait(timeout=3.0)
        app._on_tray_quick_lookup()
        wait_release.set()
        time.sleep(0.05)

        assert capture_called == []
        assert app._bridge.quick_lookup_captured.emitted == []

    def test_hotkey_reentrancy_guard_blocks_duplicate_capture(self, monkeypatch) -> None:
        app = _make_ql_app()
        gate = threading.Event()
        calls: list[int] = []

        def _slow_capture(**kw):
            calls.append(1)
            gate.wait(timeout=3.0)
            return CaptureResult(CaptureStatus.NO_TEXT)

        monkeypatch.setattr(appmod, "_wait_modifiers_released", lambda *a, **kw: True)
        monkeypatch.setattr(appmod, "capture_selection", _slow_capture)

        app._on_quick_lookup_hotkey()
        app._on_quick_lookup_hotkey()
        gate.set()
        assert app._bridge.quick_lookup_captured.event.wait(timeout=3.0)
        assert len(calls) == 1

    def test_hotkey_toggles_off_when_popup_visible(self, monkeypatch) -> None:
        app = _make_ql_app()
        app._quick_lookup_popup.visible = True
        app._quick_lookup_visible = True
        capture_called: list[bool] = []
        monkeypatch.setattr(
            appmod,
            "capture_selection",
            lambda **kw: (capture_called.append(True), CaptureResult(CaptureStatus.NO_TEXT))[1],
        )

        app._on_quick_lookup_hotkey()
        assert app._quick_lookup_popup.visible is False
        assert app._quick_lookup_visible is False
        assert app._quick_lookup_popup.hide_calls == 1
        assert capture_called == []

    def test_tray_opens_blank_popup_without_selection_capture(self, monkeypatch) -> None:
        app = _make_ql_app()
        capture_called: list[bool] = []
        monkeypatch.setattr(
            appmod,
            "capture_selection",
            lambda **kw: (capture_called.append(True), CaptureResult(CaptureStatus.NO_TEXT))[1],
        )

        app._on_tray_quick_lookup()
        assert capture_called == []
        assert app._quick_lookup_popup.opened_with == [None]

    def test_translate_falls_back_to_builtin_google_when_unconfigured(self) -> None:
        backend = _FakeBackend(result="  Eventual consistency  ")
        app = _make_ql_app(profile=None, backend=backend)

        app._on_quick_lookup_translate(1, "最终一致性", "zh2en")
        assert app._bridge.quick_lookup_result.event.wait(timeout=3.0)
        rid, translated, direction = app._bridge.quick_lookup_result.emitted[0]
        assert (rid, translated, direction) == (1, "Eventual consistency", "zh2en")
        assert backend.calls[0][1].backend_type == BUILTIN_GOOGLE.backend_type
        assert backend.calls[0][1].direction == "zh2en"

        app._on_quick_lookup_result_ready(rid, translated, direction)
        assert app._quick_lookup_popup.results == [(1, "Eventual consistency", "zh2en")]

    def test_translate_uses_latest_configured_profile_and_handles_errors(self, monkeypatch) -> None:
        monkeypatch.setattr(appmod, "_SELECTION_RETRY_DELAY_S", 0.0)
        custom = TranslationProfile(
            name="MyLLM", base_url="http://127.0.0.1:8000/v1", model="qwen", direction="zh2en"
        )
        backend = _FakeBackend(result="异步队列")
        app = _make_ql_app(profile=custom, backend=backend)

        app._on_quick_lookup_translate(2, "async queue", "en2zh")
        assert app._bridge.quick_lookup_result.event.wait(timeout=3.0)
        assert backend.calls[0][1].name == "MyLLM"
        assert backend.calls[0][1].direction == "en2zh"

        err_app = _make_ql_app(
            profile=custom,
            backend=_FakeBackend(error=TranslationError("服务不可用", status_code=503)),
        )
        err_app._on_quick_lookup_translate(3, "hello", "en2zh")
        assert err_app._bridge.quick_lookup_error.event.wait(timeout=3.0)
        rid, msg = err_app._bridge.quick_lookup_error.emitted[0]
        assert rid == 3
        assert "服务不可用" in msg
        err_app._on_quick_lookup_error_ready(rid, msg)
        assert err_app._quick_lookup_popup.errors == [(3, msg)]

    def test_cancel_hotkey_suppressed_while_popup_visible(self, qapp) -> None:
        """查词浮窗可见时全局 cancel(Esc) 不入听写队列；隐藏后恢复正常。"""
        app = _make_ql_app()
        popup = QuickLookupPopup()
        app._quick_lookup_popup = popup
        popup.visibility_changed.connect(app._on_quick_lookup_visibility_changed)

        app._on_hotkey_press("cancel")
        assert app._hotkey_queue.get_nowait() == ("press", "cancel")

        popup.open_lookup(None)
        assert app._quick_lookup_visible is True
        app._on_hotkey_press("cancel")
        assert app._hotkey_queue.empty()

        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        popup.keyPressEvent(ev)
        assert not popup.isVisible()
        assert app._quick_lookup_visible is False
        app._on_hotkey_press("cancel")
        assert app._hotkey_queue.get_nowait() == ("press", "cancel")
        popup.close()

    def test_translate_concurrency_cap_and_cancel_sync(self, monkeypatch) -> None:
        monkeypatch.setattr(appmod, "_SELECTION_RETRY_DELAY_S", 0.0)
        gate = threading.Event()
        started_count = 0
        started_two = threading.Event()
        seen_texts: list[str] = []
        seen_lock = threading.Lock()

        class _SlowBackend:
            def translate(self, text: str, profile: TranslationProfile) -> str:
                nonlocal started_count
                with seen_lock:
                    seen_texts.append(text)
                    started_count += 1
                    if started_count == 2:
                        started_two.set()
                if text in ("t1", "t2"):
                    gate.wait(timeout=3.0)
                return f"res:{text}"

        app = _make_ql_app(backend=_SlowBackend())
        app._on_quick_lookup_translate(1, "t1", "en2zh")
        app._on_quick_lookup_translate(2, "t2", "en2zh")
        assert started_two.wait(timeout=3.0)
        app._on_quick_lookup_translate(3, "t3", "en2zh")
        app._on_quick_lookup_translate(4, "t4", "en2zh")
        assert app._quick_lookup_workers == 2
        assert app._quick_lookup_pending == (4, "t4", "en2zh")

        gate.set()
        assert app._bridge.quick_lookup_result.event.wait(timeout=3.0)
        deadline = time.monotonic() + 3.0
        while app._quick_lookup_workers > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app._quick_lookup_workers == 0
        assert "t3" not in seen_texts
        assert app._bridge.quick_lookup_result.emitted == [(4, "res:t4", "en2zh")]

        app._quick_lookup_pending = (5, "t5", "en2zh")
        app._on_quick_lookup_cancelled(10)
        assert app._quick_lookup_req_seq == 10
        assert app._quick_lookup_pending is None
        assert not app._quick_lookup_is_current(4)


# ==================== 7. Win32 修饰键等待辅助 ====================


class TestModifierWaitHelper:
    def test_wait_modifiers_released_polls_until_up_or_timeout(self, monkeypatch) -> None:
        calls = {"n": 0}

        class _FakeUser32:
            def GetAsyncKeyState(self, vk: int) -> int:
                calls["n"] += 1
                if calls["n"] <= len(appmod._MODIFIER_VKS) and vk == 0x12:
                    return -32768
                return 0

        monkeypatch.setattr(appmod, "_user32", _FakeUser32())
        assert appmod._wait_modifiers_released(timeout_s=0.2, poll_interval_s=0.005) is True
        assert calls["n"] >= len(appmod._MODIFIER_VKS) + 1

        class _AlwaysHeldUser32:
            def GetAsyncKeyState(self, vk: int) -> int:
                return -32768 if vk == 0x12 else 0

        monkeypatch.setattr(appmod, "_user32", _AlwaysHeldUser32())
        assert appmod._wait_modifiers_released(timeout_s=0.02, poll_interval_s=0.005) is False

    def test_win32_ctypes_declarations_64bit_safe(self) -> None:
        import ctypes
        import sys
        from ctypes import wintypes

        if sys.platform != "win32":
            pytest.skip("仅 Windows 校验 Win32 ctypes 原型")

        assert appmod._user32 is not None
        assert appmod._user32.GetAsyncKeyState.restype is ctypes.c_short
        assert appmod._user32.GetAsyncKeyState.argtypes == [ctypes.c_int]

        # quick_lookup_popup 自身聚焦所需的 64 位安全 ctypes 声明
        assert qlmod._user32 is not None and qlmod._kernel32 is not None
        assert qlmod._user32.GetForegroundWindow.restype is wintypes.HWND
        assert qlmod._user32.GetWindowThreadProcessId.argtypes[0] is wintypes.HWND
        assert qlmod._user32.AttachThreadInput.argtypes == [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        assert qlmod._user32.AllowSetForegroundWindow.argtypes == [wintypes.DWORD]
        assert qlmod._user32.BringWindowToTop.argtypes == [wintypes.HWND]
        assert qlmod._user32.SetForegroundWindow.argtypes == [wintypes.HWND]
        assert qlmod._user32.SetFocus.argtypes == [wintypes.HWND]
        assert qlmod._kernel32.GetCurrentThreadId.restype is wintypes.DWORD

        # offscreen 环境下传入 64 位大 HWND 应安全降级返回 False 而不抛 OverflowError
        assert qlmod._focus_window_windows(0x00007FF812345678) is False
