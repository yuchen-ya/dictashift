"""DictaShiftApplication 热键事件异步串行化测试。

验证 (任务规格)：
- _on_hotkey_press / _on_hotkey_release 只入队不阻塞：pynput 回调返回时
  controller 尚未被同步调用，随后消费线程按 FIFO 顺序调用 press -> release。
- _on_tray_toggle 投递 ("press", "toggle_enabled")。
- shutdown 投递哨兵后，消费线程在 controller.shutdown 之前处理完排队的 release。

修复 C 后：当前任务 action 不再由 application 层并行跟踪（已删除 _current_action
/_action_lock），改由 controller.current_dictation_action() 查询。因此本测试的
FakeController 模拟一个可被查询的 action 字段，验证 press/release 入队语义。

装配方式：跳过 __init__（避免 ASR/recorder/hotkey/tray 等重依赖），手动装配
_hotkey_queue、_hotkey_exec_thread、_controller。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, call

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dictashift.application import DictaShiftApplication
from dictashift.settings import AppSettings


class _FakeController:
    """记录 handle_hotkey_press / release / shutdown 调用，并模拟一个可被
    current_dictation_action() 查询的当前 action 字段。

    每次调用都会阻塞一个栅栏（threading.Barrier / Event），便于测试断言
    "回调返回时 controller 尚未被调用" 以及 "随后被调到"。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (kind, action)
        self.shutdown_called = threading.Event()
        self.enabled = True
        self._lock = threading.Lock()
        # 模拟 controller 内部 _current_task.action；由 press/release 钩子维护，
        # 供 current_dictation_action() 查询（模拟真实 controller 行为）。
        self._current_action: str | None = None

    def handle_hotkey_press(self, action: str) -> None:
        with self._lock:
            self.calls.append(("press", action))
            if action in ("dictate_zh", "dictate_en"):
                self._current_action = action
            elif action == "toggle_enabled":
                self.enabled = not self.enabled

    def handle_hotkey_release(self, action: str) -> None:
        with self._lock:
            self.calls.append(("release", action))
            if self._current_action == action:
                self._current_action = None

    def current_dictation_action(self) -> str | None:
        with self._lock:
            return self._current_action

    def shutdown(self) -> None:
        self.shutdown_called.set()


def _make_app(controller: _FakeController | None = None) -> tuple[DictaShiftApplication, _FakeController]:
    """跳过 __init__，装配异步热键路径直接依赖的属性 + 启动消费线程。"""
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    ctrl = controller or _FakeController()
    app._controller = ctrl
    app.settings = AppSettings()
    app._audio_settings_pending = False
    app._recorder = SimpleNamespace(
        device_index=None, max_seconds=60, is_recording=False, set_warm_enabled=Mock(return_value=True)
    )
    app._hotkey_queue: queue.Queue = queue.Queue()
    app._hotkey_exec_thread = threading.Thread(
        target=app._hotkey_consumer_loop,
        name="dictashift-hotkey-exec-test",
        daemon=True,
    )
    app._hotkey_exec_thread.start()
    return app, ctrl


def _drain_until(ctrl: _FakeController, expected_count: int, timeout: float = 2.0) -> None:
    """等待消费线程把 ctrl.calls 处理到 expected_count 条（短超时兜底）。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if len(ctrl.calls) >= expected_count:
            return
        time.sleep(0.005)
    # 超时不再阻塞；断言会失败并给出当前 calls 状态。


# ==================== _on_hotkey_press / release 只入队不阻塞 ====================


class TestHotkeyPressReleaseAsync:
    def test_press_does_not_synchronously_call_controller(self) -> None:
        """_on_hotkey_press 返回时 controller 尚未被调用（入队即返回）。"""
        app, ctrl = _make_app()
        app._on_hotkey_press("dictate_zh")
        # 回调返回瞬间 controller 不应已被同步调用。
        assert ctrl.calls == []
        # 消费线程随后会调到。
        _drain_until(ctrl, 1)
        assert ctrl.calls == [("press", "dictate_zh")]
        # controller 已记录当前 action（经 current_dictation_action 查询）。
        assert ctrl.current_dictation_action() == "dictate_zh"
        # 清理：哨兵停消费线程。
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)

    def test_release_does_not_synchronously_call_controller(self) -> None:
        """_on_hotkey_release 返回时 controller 尚未被调用（入队即返回）。"""
        app, ctrl = _make_app()
        # 预置 controller 当前 action 为 dictate_en（模拟真实 controller 已在录）。
        ctrl._current_action = "dictate_en"
        app._on_hotkey_release("dictate_en")
        assert ctrl.calls == []
        _drain_until(ctrl, 1)
        assert ctrl.calls == [("release", "dictate_en")]
        # controller 内 action 已清除。
        assert ctrl.current_dictation_action() is None
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)

    def test_press_release_executed_in_fifo_order(self) -> None:
        """连续 press -> release 入队后，消费线程按 FIFO 顺序执行 press 再 release。"""
        app, ctrl = _make_app()
        app._on_hotkey_press("dictate_zh")
        app._on_hotkey_release("dictate_zh")
        _drain_until(ctrl, 2)
        assert ctrl.calls == [("press", "dictate_zh"), ("release", "dictate_zh")]
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)

    def test_press_records_action_in_controller(self) -> None:
        """press 经消费线程调到 controller 后，controller 记录当前 action
        （dictate_zh/dictate_en 记录，toggle_enabled 不记录）。"""
        app, ctrl = _make_app()
        app._on_hotkey_press("dictate_zh")
        _drain_until(ctrl, 1)
        assert ctrl.current_dictation_action() == "dictate_zh"
        # toggle_enabled 不覆盖当前 action（真实 controller 不接受新听写任务时不变）。
        app._on_hotkey_press("toggle_enabled")
        _drain_until(ctrl, 2)
        assert ctrl.current_dictation_action() == "dictate_zh"
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)


# ==================== _on_tray_toggle ====================


class TestTrayToggleEnqueues:
    def test_tray_toggle_enqueues_press_toggle_enabled(self) -> None:
        """_on_tray_toggle 入队 ("press", "toggle_enabled")，不直接调 controller。"""
        app, ctrl = _make_app()
        app._on_tray_toggle()
        assert ctrl.calls == []  # 回调返回瞬间未同步调用
        _drain_until(ctrl, 1)
        assert ctrl.calls == [("press", "toggle_enabled")]
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)


# ==================== shutdown 处理完排队事件 ====================


class TestShutdownDrainsQueue:
    def test_shutdown_processes_queued_release_before_controller_shutdown(self) -> None:
        """shutdown 投递哨兵并 join 后，排队的 release 已被消费线程处理完。

        场景：先 release 入队（未处理），再 shutdown -> 哨兵排在 release 之后，
        消费线程在退出前先处理 release。这里直接验证 join 后 ctrl.calls 包含 release。
        """
        app, ctrl = _make_app()
        # 预置 controller 当前 action（模拟 release 能取到的状态）。
        ctrl._current_action = "dictate_zh"
        # release 入队（消费线程尚未处理）。
        app._on_hotkey_release("dictate_zh")
        # 模拟 shutdown：hotkeys.stop() 是空操作（这里没有 hotkey service），
        # 直接投递哨兵 + join，再调 controller.shutdown。
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)
        # 消费线程退出前已处理 release。
        assert ctrl.calls == [("release", "dictate_zh")]
        assert not app._hotkey_exec_thread.is_alive()

    def test_shutdown_join_completes_within_timeout(self) -> None:
        """空队列下 shutdown 投递哨兵后 join 在 2s 内返回。"""
        app, ctrl = _make_app()
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)
        assert not app._hotkey_exec_thread.is_alive()
        assert ctrl.calls == []


# ==================== 音频事件走热键队列 ====================


def test_audio_events_keep_recording_sequence() -> None:
    task = SimpleNamespace(action="dictate_en", seq=7)
    calls = []
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._hotkey_queue = queue.Queue()
    app._audio_settings_pending = False
    app._controller = SimpleNamespace(
        current_dictation_task=lambda: task,
        handle_hotkey_release=lambda action, *, expected_seq: calls.append(("release", action, expected_seq)),
        _on_first_frame=lambda *, expected_seq: calls.append(("first_frame", expected_seq)),
        _on_stream_error=lambda message, *, expected_seq: calls.append(("stream_error", message, expected_seq)),
    )

    app._on_recorder_first_frame()
    app._on_stream_error("device error")
    app._on_auto_stop()
    app._hotkey_queue.put(None)
    app._hotkey_consumer_loop()

    assert calls == [("first_frame", 7), ("stream_error", "device error", 7), ("release", "dictate_en", 7)]


def test_audio_events_after_recording_ended_are_ignored() -> None:
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._hotkey_queue = queue.Queue()
    app._audio_settings_pending = False
    app._controller = SimpleNamespace(current_dictation_task=lambda: None)
    app._on_recorder_first_frame()
    app._on_stream_error("device error")
    app._on_auto_stop()

    assert app._hotkey_queue.empty()


def test_instant_recording_turns_off_on_pause_and_back_on_on_resume() -> None:
    app, ctrl = _make_app()
    app.settings.asr.instant_recording = True
    app._hotkey_queue.put(("audio_settings", True))
    app._on_tray_toggle()
    app._on_tray_toggle()
    app._hotkey_queue.put(None)
    app._hotkey_exec_thread.join(timeout=2)

    assert ctrl.enabled
    assert app._recorder.set_warm_enabled.call_args_list == [call(True), call(False), call(True)]
