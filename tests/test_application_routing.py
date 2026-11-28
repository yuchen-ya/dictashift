"""DictaShiftApplication 错误/结果路由单元测试。

测试目标：
- _on_error 按 settings.ui.result_ui 路由：overlay 模式调 overlay.show_error，
  否则调 result_popup.show_error；两种模式都保留 tray.show_message 与 overlay.set_state(ERROR)。
- _on_text_ready 按 result_ui 路由：
  - overlay 模式：直输成功（pasted=True）不打扰，直接 return（悬浮窗不显示结果）；
    直输失败（pasted=False）调 overlay.show_result(text, failed=True)。
  - popup 模式：pasted=True 直接 return；pasted=False 调 result_popup.show_result。

不构建完整 DictaShiftApplication（避免 ASR/recorder/hotkey/tray 等重依赖）。
用 __new__ 跳过 __init__，仅装配 _on_error/_on_text_ready 直接读取的属性：
self.settings.ui.result_ui、self._overlay、self._result_popup、self._tray。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dictashift.application import DictaShiftApplication
from dictashift.core.state import AppState
from dictashift.settings.models import AppSettings, UiSettings


class _FakeOverlay:
    """记录 show_result / show_error / set_state 调用的 overlay 替身。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []  # (text, failed)
        self.errors: list[str] = []
        self.states: list[AppState] = []

    def show_result(self, text: str, failed: bool = True) -> None:
        self.results.append((text, failed))

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def set_state(self, state: AppState) -> None:
        self.states.append(state)


class _FakePopup:
    """记录 show_result / show_error 调用的 ResultPopup 替身。"""

    def __init__(self) -> None:
        self.results: list[str] = []
        self.errors: list[str] = []

    def show_result(self, text: str) -> None:
        self.results.append(text)

    def show_error(self, message: str) -> None:
        self.errors.append(message)


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def show_message(self, title: str, body: str) -> None:
        self.messages.append((title, body))


def _make_app(result_ui: str) -> tuple[DictaShiftApplication, _FakeOverlay, _FakePopup, _FakeTray]:
    """跳过 __init__，仅装配路由方法直接依赖的属性。"""
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._overlay = _FakeOverlay()
    app._result_popup = _FakePopup()
    app._tray = _FakeTray()
    settings = AppSettings()
    settings.ui = UiSettings(result_ui=result_ui)
    app.settings = settings
    return app, app._overlay, app._result_popup, app._tray


# ==================== _on_error 路由 ====================


def test_on_error_popup_mode_routes_to_popup() -> None:
    """result_ui=popup 时 _on_error 调 result_popup.show_error（并保留 tray/overlay 状态）。"""
    app, overlay, popup, tray = _make_app("popup")
    app._on_error("boom")
    assert popup.errors == ["boom"]
    assert overlay.errors == []  # popup 模式不调 overlay.show_error
    # 仍保留 tray 通知与 overlay 状态置 ERROR
    assert tray.messages == [("DictaShift", "boom")]
    assert overlay.states == [AppState.ERROR]


def test_on_error_overlay_mode_routes_to_overlay() -> None:
    """result_ui=overlay 时 _on_error 调 overlay.show_error（并不弹 popup）。"""
    app, overlay, popup, tray = _make_app("overlay")
    app._on_error("err-x")
    assert overlay.errors == ["err-x"]
    assert popup.errors == []
    # tray 与 overlay 状态置 ERROR 仍保留
    assert tray.messages == [("DictaShift", "err-x")]
    assert overlay.states == [AppState.ERROR]


# ==================== _on_text_ready 路由 ====================


def test_on_text_ready_popup_success_does_not_disturb() -> None:
    """popup 模式 + pasted=True：直接 return，不打扰用户。"""
    app, overlay, popup, tray = _make_app("popup")
    app._on_text_ready("hello", True)
    assert popup.results == []
    assert overlay.results == []


def test_on_text_ready_popup_failure_shows_popup() -> None:
    """popup 模式 + pasted=False：弹 ResultPopup.show_result。"""
    app, overlay, popup, tray = _make_app("popup")
    app._on_text_ready("hello", False)
    assert popup.results == ["hello"]
    assert overlay.results == []


def test_on_text_ready_overlay_success_does_not_show() -> None:
    """overlay 模式 + pasted=True：直输成功，悬浮窗不显示结果（直接 return）。"""
    app, overlay, popup, tray = _make_app("overlay")
    app._on_text_ready("hello", True)
    assert overlay.results == []  # 不调 show_result
    assert popup.results == []


def test_on_text_ready_overlay_failure_shows_result() -> None:
    """overlay 模式 + pasted=False：show_result(failed=True) 在悬浮窗显示文本。"""
    app, overlay, popup, tray = _make_app("overlay")
    app._on_text_ready("hello", False)
    assert overlay.results == [("hello", True)]
    assert popup.results == []


# ==================== _on_test_connection 回归 ====================


class _FakeBackend:
    def __init__(self, result: str = "  hello  ") -> None:
        self._result = result
        self.calls: list[tuple[str, object]] = []

    def translate(self, text: str, profile) -> str:
        self.calls.append((text, profile))
        return self._result


class _FakeSignal:
    """记录 emit 并通知等待者的 signal 替身。"""

    def __init__(self) -> None:
        import threading

        self.emitted: list[tuple] = []
        self.event = threading.Event()

    def emit(self, *args) -> None:
        self.emitted.append(args)
        self.event.set()


class _FakeBridge:
    def __init__(self) -> None:
        self.test_conn_result = _FakeSignal()
        self.models_fetched = _FakeSignal()


class _FakeSettingsWindow:
    def __init__(self) -> None:
        self.fetched_calls: list[tuple[bool, list[str], str, bool, str | None]] = []

    def set_fetched_models(
        self,
        ok: bool,
        models: list[str],
        message: str,
        silent: bool = False,
        base_url: str | None = None,
    ) -> None:
        self.fetched_calls.append((ok, models, message, silent, base_url))


def test_on_test_connection_runs_worker_and_emits_result() -> None:
    """「测试连接」应在后台线程调 backend.translate 并 emit 结果（回归：线程对象
    曾未赋值给 t，t.start() 直接 NameError，按钮一点就崩）。"""
    from dictashift.translation import TranslationProfile

    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._backend = _FakeBackend()
    app._bridge = _FakeBridge()
    profile = TranslationProfile(
        name="t", backend_type="openai", base_url="http://unused/v1",
        model="m", api_key=None, timeout=5.0,
    )

    # 不应抛 NameError（回归点）；worker 线程执行 translate 并 emit。
    app._on_test_connection(profile)

    assert app._bridge.test_conn_result.event.wait(timeout=5.0)
    assert app._backend.calls and app._backend.calls[0][0] == "你好"
    ok, msg = app._bridge.test_conn_result.emitted[0]
    assert ok is True
    assert msg == "hello"


def test_fetch_models_runs_in_background_and_drops_stale_seq(monkeypatch) -> None:
    """后台拉取可用模型并通过递增序号丢弃乱序过期结果。"""
    import threading
    from dictashift import application as app_mod

    monkeypatch.setattr(
        app_mod,
        "fetch_available_models",
        lambda base_url, api_key=None: ["gpt-4o-mini", "gpt-5"],
    )

    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._bridge = _FakeBridge()
    app._settings_window = _FakeSettingsWindow()
    app._fetch_models_seq = 0
    app._fetch_models_lock = threading.Lock()

    app._on_fetch_models_requested("https://api.openai.com/v1", "sk-x", False)
    assert app._bridge.models_fetched.event.wait(timeout=5.0)
    seq, ok, models, msg, silent, base_url = app._bridge.models_fetched.emitted[0]
    assert seq == 1
    assert ok is True
    assert models == ["gpt-4o-mini", "gpt-5"]
    assert silent is False
    assert base_url == "https://api.openai.com/v1"

    # 模拟旧序号迟到：推进当前序号到 2 后，seq=1 应被丢弃
    app._fetch_models_seq = 2
    app._on_models_fetched(seq, ok, models, msg, silent, base_url)
    assert app._settings_window.fetched_calls == []

    # 最新序号 2 正常回填（附带 base_url）
    app._on_models_fetched(2, True, ["qwen3-8b"], "", True, "http://127.0.0.1:19120/v1")
    assert app._settings_window.fetched_calls == [
        (True, ["qwen3-8b"], "", True, "http://127.0.0.1:19120/v1")
    ]


def test_settings_window_set_fetched_models_ignores_mismatched_base_url() -> None:
    """快速切换自定义档案时，旧 Base URL 迟到的 /models 结果不应污染当前下拉框。"""
    from PySide6.QtWidgets import QApplication
    from dictashift.translation import TranslationProfile
    from dictashift.ui.settings_window import SettingsWindow

    _app = QApplication.instance() or QApplication([])
    win = SettingsWindow()
    s = AppSettings()
    s.translation.profiles = [
        TranslationProfile(name="A", base_url="https://api-a.example/v1", model="model-a"),
        TranslationProfile(name="B", base_url="https://api-b.example/v1", model="model-b"),
    ]
    s.translation.active_profile = "B"
    win.load_from_settings(s)

    # 迟到的 A 档案结果应被忽略
    win.set_fetched_models(
        True, ["stale-model-a"], "", silent=True, base_url="https://api-a.example/v1"
    )
    items = [win._model_combo.itemText(i) for i in range(win._model_combo.count())]
    assert "stale-model-a" not in items
    assert win._model_edit.text() == "model-b"

    # 匹配 B 档案的结果正常填充
    win.set_fetched_models(
        True, ["model-b", "model-b-pro"], "", silent=True, base_url="https://api-b.example/v1"
    )
    items = [win._model_combo.itemText(i) for i in range(win._model_combo.count())]
    assert items == ["model-b", "model-b-pro"]
    win.close()
