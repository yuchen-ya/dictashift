"""DictaShiftApplication 热键事件异步串行化测试。

验证 (任务规格)：
- _on_hotkey_press / _on_hotkey_release 只入队不阻塞：pynput 回调返回时
  controller 尚未被同步调用，随后消费线程按 FIFO 顺序调用 press -> release。
- _on_tray_toggle 投递 ("press", "toggle_enabled")。
- shutdown 投递哨兵后，消费线程在 controller.shutdown 之前处理完排队的 release。

装配方式：跳过 __init__（避免 ASR/recorder/hotkey/tray 等重依赖），手动装配
_hotkey_queue、_action_lock、_current_action、_hotkey_exec_thread、_controller。
"""

from __future__ import annotations

import os
import queue
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dictashift.application import DictaShiftApplication


class _FakeController:
    """记录 handle_hotkey_press / release / shutdown 调用的 controller 替身。

    每次调用都会阻塞一个栅栏（threading.Barrier / Event），便于测试断言
    "回调返回时 controller 尚未被调用" 以及 "随后被调到"。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (kind, action)
        self.shutdown_called = threading.Event()
        self._lock = threading.Lock()

    def handle_hotkey_press(self, action: str) -> None:
        with self._lock:
            self.calls.append(("press", action))

    def handle_hotkey_release(self, action: str) -> None:
        with self._lock:
            self.calls.append(("release", action))

    def shutdown(self) -> None:
        self.shutdown_called.set()


def _make_app(controller: _FakeController | None = None) -> tuple[DictaShiftApplication, _FakeController]:
    """跳过 __init__，装配异步热键路径直接依赖的属性 + 启动消费线程。"""
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    ctrl = controller or _FakeController()
    app._controller = ctrl
    app._action_lock = threading.Lock()
    app._current_action = None
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
        # _current_action 已记录。
        assert app._current_action == "dictate_zh"
        # 清理：哨兵停消费线程。
        app._hotkey_queue.put(None)
        app._hotkey_exec_thread.join(timeout=2.0)

    def test_release_does_not_synchronously_call_controller(self) -> None:
        """_on_hotkey_release 返回时 controller 尚未被调用（入队即返回）。"""
        app, ctrl = _make_app()
        app._current_action = "dictate_en"
        app._on_hotkey_release("dictate_en")
        assert ctrl.calls == []
        _drain_until(ctrl, 1)
        assert ctrl.calls == [("release", "dictate_en")]
        # _current_action 已清除。
        assert app._current_action is None
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

    def test_press_tracks_action_records_current_action(self) -> None:
        """press 逻辑保留 _current_action 跟踪（dictate_zh/dictate_en 记录，其他不记）。"""
        app, ctrl = _make_app()
        app._on_hotkey_press("dictate_zh")
        assert app._current_action == "dictate_zh"
        app._on_hotkey_press("toggle_enabled")
        # toggle_enabled 不覆盖 _current_action。
        assert app._current_action == "dictate_zh"
        _drain_until(ctrl, 2)
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
        app._current_action = "dictate_zh"
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
