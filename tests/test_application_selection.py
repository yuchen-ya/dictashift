"""划词翻译装配层接线测试（不构建完整 DictaShiftApplication）。

测试目标：
- 热键 action 路由：selection_translate（划词翻译开关）press -> emit
  selection_toggle（queued 到主线程翻转启停），且不入 _hotkey_queue
  （不进入 controller 听写流水线）；release 完全忽略。
- F7 开关槽：_on_selection_toggle 翻转 settings.selection_translate.enabled、
  经 _apply_selection_settings 启停服务、repo.save 落盘、托盘气泡反馈。
- _on_selection_result 分支：success -> 后台翻译 -> result emit
  （方向经 detect_direction 注入 profile 副本；无 loading 信号）；
  too_long -> error emit；no_text / skipped_self 静默；error -> error emit；
  无活动 profile / 空响应 -> error emit。
- 网络错误自动重试一次：首次抛异常 -> 间隔重试 -> 成功出结果；
  两次都失败 -> error emit（重试间隔 monkeypatch 加速）。
- 过期请求丢弃：慢翻译被新请求取代后，旧结果不 emit。
- 按压活动：_on_press_activity 立即 popup.hide()。
- _bindings_from_settings：默认配置包含 selection_translate 绑定 F7；
  旧配置（hotkeys 缺 selection_translate 键）回退 F7。
- _apply_selection_settings：enabled 启停 service、auto_trigger / popup 时长热更新。

用 __new__ 跳过 __init__，仅装配被测方法直接读取的属性
（与 test_application_routing.py 同模式）。
"""

from __future__ import annotations

import os
import queue
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dictashift.application import DictaShiftApplication
from dictashift.selection.capture import CaptureResult, CaptureStatus
from dictashift.settings.models import AppSettings, SelectionTranslateSettings
from dictashift.translation import TranslationError, TranslationProfile


# ==================== fake 依赖 ====================


class _FakeSignal:
    """记录 emit 并通知等待者的 signal 替身。"""

    def __init__(self) -> None:
        self.emitted: list[tuple] = []
        self.event = threading.Event()

    def emit(self, *args) -> None:
        self.emitted.append(args)
        self.event.set()


class _FakeBridge:
    def __init__(self) -> None:
        self.selection_toggle = _FakeSignal()
        self.press_activity = _FakeSignal()
        self.selection_result = _FakeSignal()
        self.selection_error = _FakeSignal()


class _FakeSelectionService:
    """记录生命周期与配置调用的 SelectionService 替身。"""

    def __init__(self) -> None:
        self.started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.auto_trigger_history: list[bool] = []

    def start(self) -> None:
        self.start_calls += 1
        self.started = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.started = False

    def set_auto_trigger(self, enabled: bool) -> None:
        self.auto_trigger_history.append(bool(enabled))


class _FakePopup:
    def __init__(self) -> None:
        self.results: list[str] = []
        self.errors: list[str] = []
        self.timeouts: list[int] = []
        self.hide_calls = 0

    def show_result(self, text: str) -> None:
        self.results.append(text)

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def set_auto_close_seconds(self, seconds: int) -> None:
        self.timeouts.append(int(seconds))

    def hide(self) -> None:
        self.hide_calls += 1


class _FakeController:
    def __init__(self, profile: TranslationProfile | None) -> None:
        self._profile = profile

    def get_active_profile(self) -> TranslationProfile | None:
        return self._profile


class _FakeBackend:
    """记录调用并返回预设译文的 backend 替身。"""

    def __init__(self, result: str = "translated") -> None:
        self._result = result
        self.calls: list[tuple[str, TranslationProfile]] = []

    def translate(self, text: str, profile: TranslationProfile) -> str:
        self.calls.append((text, profile))
        return self._result


class _GatedBackend:
    """第一次调用阻塞直到 gate 放开（模拟慢翻译），后续调用立即返回。"""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.first_started = threading.Event()
        self.first_returned = threading.Event()
        self.calls: list[str] = []

    def translate(self, text: str, profile: TranslationProfile) -> str:
        self.calls.append(text)
        if len(self.calls) == 1:
            self.first_started.set()
            self.gate.wait(timeout=5.0)
            self.first_returned.set()
            return "r1-stale"
        return "r2-fresh"


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def show_message(self, title: str, body: str) -> None:
        self.messages.append((title, body))


class _FakeRepo:
    """记录 save 调用的 SettingsRepository 替身。"""

    def __init__(self) -> None:
        self.saved: list[AppSettings] = []

    def save(self, settings: AppSettings) -> None:
        self.saved.append(settings)


_PROFILE = TranslationProfile(
    name="p1", base_url="http://x/v1", model="m", direction="zh2en"
)


def _make_app(
    *,
    profile: TranslationProfile | None = _PROFILE,
    backend=None,
    started: bool = True,
) -> DictaShiftApplication:
    """跳过 __init__，仅装配划词接线方法直接依赖的属性。"""
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._bridge = _FakeBridge()
    app._selection = _FakeSelectionService()
    app._selection_popup = _FakePopup()
    app._controller = _FakeController(profile)
    app._backend = backend if backend is not None else _FakeBackend()
    app._tray = _FakeTray()
    app._repo = _FakeRepo()
    app._selection_started = started
    app._selection_seq = 0
    app._selection_seq_lock = threading.Lock()
    app._hotkey_queue = queue.Queue()
    app.settings = AppSettings()
    return app


def _success(text: str) -> CaptureResult:
    return CaptureResult(CaptureStatus.SUCCESS, text=text, length=len(text))


# ==================== 热键 action 路由（F7 = 划词翻译开关） ====================


class TestHotkeyRouting:
    def test_press_emits_toggle_signal_not_controller_queue(self) -> None:
        """selection_translate press -> emit selection_toggle；不入 _hotkey_queue。"""
        app = _make_app()
        app._on_hotkey_press("selection_translate")
        assert app._bridge.selection_toggle.emitted == [()]
        assert app._hotkey_queue.empty()  # 不进入 controller 听写流水线

    def test_press_emits_toggle_regardless_of_service_state(self) -> None:
        """开关语义与服务运行状态无关（未运行时按下 = 开启）。"""
        app = _make_app(started=False)
        app._on_hotkey_press("selection_translate")
        assert app._bridge.selection_toggle.emitted == [()]
        assert app._hotkey_queue.empty()

    def test_release_is_noop(self) -> None:
        """selection_translate 无松开语义：release 完全忽略（防御）。"""
        app = _make_app()
        app._on_hotkey_release("selection_translate")
        assert app._hotkey_queue.empty()
        assert app._bridge.selection_toggle.emitted == []

    def test_dictate_actions_unaffected(self) -> None:
        """既有 dictate action 仍走 _hotkey_queue（路由改动不影响听写流水线）。"""
        app = _make_app()
        app._on_hotkey_press("dictate_zh")
        assert app._hotkey_queue.get_nowait() == ("press", "dictate_zh")


# ==================== F7 开关槽（主线程） ====================


class TestSelectionToggle:
    def test_toggle_enables_and_persists(self) -> None:
        """关闭态按下 F7：翻转 enabled -> start 服务 -> 落盘 -> 托盘「已开启」。"""
        app = _make_app(started=False)
        app.settings.selection_translate = SelectionTranslateSettings(enabled=False)
        app._on_selection_toggle()
        assert app.settings.selection_translate.enabled is True
        # 服务启停被调用（经 _apply_selection_settings）。
        assert app._selection.start_calls == 1
        assert app._selection_started is True
        # 落盘（与 _on_settings_applied 同款的 repo.save）。
        assert len(app._repo.saved) == 1
        assert app._repo.saved[0].selection_translate.enabled is True
        # 托盘气泡反馈。
        assert app._tray.messages == [("DictaShift", "划词翻译已开启")]

    def test_toggle_disables_and_persists(self) -> None:
        """开启态按下 F7：翻转 enabled -> stop 服务 -> 落盘 -> 托盘「已关闭」。"""
        app = _make_app(started=True)
        app._selection.started = True  # fake 初始未 start，这里对齐为运行态
        app.settings.selection_translate = SelectionTranslateSettings(enabled=True)
        app._on_selection_toggle()
        assert app.settings.selection_translate.enabled is False
        assert app._selection.stop_calls == 1
        assert app._selection_started is False
        assert len(app._repo.saved) == 1
        assert app._tray.messages == [("DictaShift", "划词翻译已关闭")]

    def test_toggle_twice_roundtrip(self) -> None:
        """连续两次 F7：回到原状态，两次都落盘。"""
        app = _make_app(started=False)
        app.settings.selection_translate = SelectionTranslateSettings(enabled=False)
        app._on_selection_toggle()
        app._selection.started = True
        app._on_selection_toggle()
        assert app.settings.selection_translate.enabled is False
        assert len(app._repo.saved) == 2


class TestBindingsFromSettings:
    def test_default_binding_f7(self) -> None:
        """默认配置包含 selection_translate 绑定 F7。"""
        bindings = DictaShiftApplication._bindings_from_settings(AppSettings())
        by_action = {b.action: b.keys for b in bindings}
        assert by_action["selection_translate"] == frozenset({"f7"})
        # 既有 action 不受影响。
        assert by_action["dictate_zh"] == frozenset({"f8"})
        assert by_action["cancel"] == frozenset({"esc"})

    def test_old_config_without_selection_key_falls_back_f7(self) -> None:
        """旧 settings.json（hotkeys 无 selection_translate 键）回退 F7。"""
        old = AppSettings.from_dict({"hotkeys": {"dictate_zh": "f8"}})
        bindings = DictaShiftApplication._bindings_from_settings(old)
        by_action = {b.action: b.keys for b in bindings}
        assert by_action["selection_translate"] == frozenset({"f7"})

    def test_invalid_selection_hotkey_skipped(self) -> None:
        """非法快捷键配置跳过该 binding（其余不受影响）。"""
        s = AppSettings()
        s.hotkeys.selection_translate = "ctrl++f7"
        bindings = DictaShiftApplication._bindings_from_settings(s)
        by_action = {b.action: b.keys for b in bindings}
        assert "selection_translate" not in by_action
        assert by_action["dictate_zh"] == frozenset({"f8"})


# ==================== _on_selection_result 分支 ====================


class TestOnSelectionResult:
    def test_success_emits_result_zh2en(self) -> None:
        """中文文本 -> zh2en 方向注入 profile 副本 -> 译文经 result 信号发出。"""
        backend = _FakeBackend("你好-translated")
        app = _make_app(backend=backend)
        app._on_selection_result(_success("你好世界"))
        assert app._bridge.selection_result.event.wait(timeout=5.0)
        assert app._bridge.selection_result.emitted == [("你好-translated",)]
        # 方向经 detect_direction 注入（dataclasses.replace 生成副本，原 profile 不动）。
        assert backend.calls[0][0] == "你好世界"
        assert backend.calls[0][1].direction == "zh2en"
        assert backend.calls[0][1] is not _PROFILE
        assert _PROFILE.direction == "zh2en"

    def test_success_english_text_en2zh(self) -> None:
        """英文文本 -> en2zh 方向。"""
        backend = _FakeBackend("你好")
        app = _make_app(backend=backend)
        app._on_selection_result(_success("hello world"))
        assert app._bridge.selection_result.event.wait(timeout=5.0)
        assert backend.calls[0][1].direction == "en2zh"

    def test_no_active_profile_shows_error(self) -> None:
        """无活动翻译档案 -> error 提示（不发起翻译调用）。"""
        backend = _FakeBackend()
        app = _make_app(profile=None, backend=backend)
        app._on_selection_result(_success("你好"))
        assert app._bridge.selection_error.event.wait(timeout=5.0)
        (msg,) = app._bridge.selection_error.emitted[0]
        assert "未配置翻译服务" in msg
        assert app._bridge.selection_result.emitted == []
        assert backend.calls == []

    def test_empty_translation_shows_error(self) -> None:
        """空响应 -> error 提示而非空弹窗。"""
        app = _make_app(backend=_FakeBackend("   "))
        app._on_selection_result(_success("你好"))
        assert app._bridge.selection_error.event.wait(timeout=5.0)
        (msg,) = app._bridge.selection_error.emitted[0]
        assert "空响应" in msg
        assert app._bridge.selection_result.emitted == []

    def test_too_long_shows_error(self) -> None:
        backend = _FakeBackend()
        app = _make_app(backend=backend)
        app._on_selection_result(
            CaptureResult(CaptureStatus.TOO_LONG, reason="选中文本过长 (3000 > 2000 字符)", length=3000)
        )
        assert app._bridge.selection_error.emitted == [("选中文本过长 (3000 > 2000 字符)",)]
        # 不进入翻译流程。
        assert backend.calls == []

    @pytest.mark.parametrize("status", [CaptureStatus.NO_TEXT, CaptureStatus.SKIPPED_SELF])
    def test_no_text_and_skipped_self_silent(self, status: CaptureStatus) -> None:
        """no_text / skipped_self：静默（仅日志），无任何信号。"""
        backend = _FakeBackend()
        app = _make_app(backend=backend)
        app._on_selection_result(CaptureResult(status, reason="r"))
        assert app._bridge.selection_result.emitted == []
        assert app._bridge.selection_error.emitted == []
        assert backend.calls == []

    def test_capture_error_shows_error(self) -> None:
        """取词 error：可见提示（用户触发了功能，静默会像「坏了」）。"""
        app = _make_app()
        app._on_selection_result(CaptureResult(CaptureStatus.ERROR, reason="SendInput boom"))
        (msg,) = app._bridge.selection_error.emitted[0]
        assert "取词失败" in msg
        assert "SendInput boom" in msg


# ==================== 网络错误自动重试一次 ====================


class TestTranslateRetry:
    def test_first_failure_retry_succeeds(self, monkeypatch) -> None:
        """首次抛异常（瞬断）-> 间隔重试一次 -> 成功出结果；共两次调用。"""
        # 重试间隔加速：不打乱真实等待。
        monkeypatch.setattr("dictashift.application.time.sleep", lambda _s: None)

        class _FlakyBackend:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, text, profile):
                self.calls += 1
                if self.calls == 1:
                    raise TranslationError("connection closed", status_code=None)
                return "retried-ok"

        backend = _FlakyBackend()
        app = _make_app(backend=backend)
        app._on_selection_result(_success("你好"))
        assert app._bridge.selection_result.event.wait(timeout=5.0)
        assert backend.calls == 2
        assert app._bridge.selection_result.emitted == [("retried-ok",)]
        assert app._bridge.selection_error.emitted == []

    def test_both_attempts_fail_shows_error(self, monkeypatch) -> None:
        """两次都失败 -> error 提示（含异常类型名），不向外抛；共两次调用。"""
        monkeypatch.setattr("dictashift.application.time.sleep", lambda _s: None)

        class _BoomBackend:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, text, profile):
                self.calls += 1
                raise TranslationError("rate limited", status_code=429)

        backend = _BoomBackend()
        app = _make_app(backend=backend)
        app._on_selection_result(_success("你好"))
        assert app._bridge.selection_error.event.wait(timeout=5.0)
        assert backend.calls == 2
        (msg,) = app._bridge.selection_error.emitted[0]
        assert "TranslationError" in msg
        assert app._bridge.selection_result.emitted == []


# ==================== 过期请求丢弃 ====================


class TestStaleResultDropped:
    def test_slow_stale_translation_dropped(self) -> None:
        """慢翻译被新请求取代后，旧结果不 emit（只有新结果到达）。"""
        backend = _GatedBackend()
        app = _make_app(backend=backend)

        app._on_selection_result(_success("第一次"))
        # 等第一次翻译在 worker 内阻塞（确保 seq 检查发生在第二次触发之后）。
        assert backend.first_started.wait(timeout=5.0)
        app._on_selection_result(_success("第二次"))
        # 放开第一次翻译；两次都结束后断言。
        backend.gate.set()
        assert backend.first_returned.wait(timeout=5.0)
        assert app._bridge.selection_result.event.wait(timeout=5.0)

        assert app._bridge.selection_result.emitted == [("r2-fresh",)]
        assert app._bridge.selection_error.emitted == []


# ==================== 主线程槽（queued signal 的接收端） ====================


class TestUiSlots:
    def test_slots_drive_popup(self) -> None:
        app = _make_app()
        app._on_selection_result_ready("译文")
        app._on_selection_error_ready("出错了")
        assert app._selection_popup.results == ["译文"]
        assert app._selection_popup.errors == ["出错了"]

    def test_press_activity_hides_popup(self) -> None:
        """任意左键按下（queued 到主线程）-> 立即收起旧弹窗。"""
        app = _make_app()
        app._on_press_activity()
        app._on_press_activity()
        assert app._selection_popup.hide_calls == 2


# ==================== _apply_selection_settings ====================


class TestApplySelectionSettings:
    def test_enable_starts_service(self) -> None:
        app = _make_app(started=False)
        app.settings.selection_translate = SelectionTranslateSettings(enabled=True)
        app._apply_selection_settings()
        assert app._selection.start_calls == 1
        assert app._selection_started is True

    def test_disable_stops_service(self) -> None:
        app = _make_app(started=True)
        app._selection.started = True  # fake 初始未 start，这里对齐为运行态
        app.settings.selection_translate = SelectionTranslateSettings(enabled=False)
        app._apply_selection_settings()
        assert app._selection.stop_calls == 1
        assert app._selection_started is False

    def test_enable_twice_does_not_double_start(self) -> None:
        """已运行时重复 apply 不再 start（SelectionService.start 重复会抛）。"""
        app = _make_app(started=False)
        app.settings.selection_translate = SelectionTranslateSettings(enabled=True)
        app._apply_selection_settings()
        assert app._selection.start_calls == 1
        app._apply_selection_settings()
        assert app._selection.start_calls == 1  # 未重复 start

    def test_auto_trigger_and_timeout_propagated(self) -> None:
        app = _make_app(started=False)
        app.settings.selection_translate = SelectionTranslateSettings(
            enabled=False, auto_trigger=False, popup_timeout_s=25
        )
        app._apply_selection_settings()
        assert app._selection.auto_trigger_history == [False]
        assert app._selection_popup.timeouts == [25]

    def test_start_failure_shows_tray_message(self) -> None:
        """service.start 抛异常：托盘提示，_selection_started 保持 False。"""
        app = _make_app(started=False)

        def boom():
            raise RuntimeError("listener fail")

        app._selection.start = boom
        app.settings.selection_translate = SelectionTranslateSettings(enabled=True)
        app._apply_selection_settings()
        assert app._selection_started is False
        assert app._tray.messages == [("DictaShift", "划词翻译监听启动失败")]

    def test_stop_selection_service_idempotent(self) -> None:
        """_stop_selection_service 幂等：未运行时调用安全。"""
        app = _make_app(started=False)
        app._stop_selection_service()
        assert app._selection_started is False
