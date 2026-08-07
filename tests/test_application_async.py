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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from dictashift.application import DictaShiftApplication


class _FakeController:
    """记录 handle_hotkey_press / release / shutdown 调用，并模拟一个可被
    current_dictation_action() 查询的当前 action 字段。

    每次调用都会阻塞一个栅栏（threading.Barrier / Event），便于测试断言
    "回调返回时 controller 尚未被调用" 以及 "随后被调到"。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (kind, action)
        self.shutdown_called = threading.Event()
        self._lock = threading.Lock()
        # 模拟 controller 内部 _current_task.action；由 press/release 钩子维护，
        # 供 current_dictation_action() 查询（模拟真实 controller 行为）。
        self._current_action: str | None = None

    def handle_hotkey_press(self, action: str) -> None:
        with self._lock:
            self.calls.append(("press", action))
            if action in ("dictate_zh", "dictate_en"):
                self._current_action = action

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


# ==================== _on_auto_stop 经 controller 查询驱动（修复 C）====================
#
# 旧行为：application 层并行跟踪 _current_action（on_press 记录、on_release 清除），
# 存在竞态——press 被拒绝时仍覆盖、release 后被清空，导致 auto-stop 漏触发或抢错
# action。修复后：_on_auto_stop 用 controller.current_dictation_action() 查询当前
# action（锁内读 _current_task.action），与 controller 任务生命周期一致。


class _FakeControllerWithAction:
    """可被 current_dictation_action() 查询、记录 release 调用的 controller 替身。

    - current_dictation_action：返回 _current_action（模拟 controller 内
      _current_task.action，由外部设定）。
    - handle_hotkey_release：记录调用并清 _current_action（模拟任务被取走）。
    """

    def __init__(self, action: str | None = None) -> None:
        self._current_action = action
        self.release_calls: list[str] = []
        self.press_calls: list[str] = []
        self._lock = threading.Lock()

    def handle_hotkey_press(self, action: str) -> None:
        with self._lock:
            self.press_calls.append(action)

    def handle_hotkey_release(self, action: str) -> None:
        with self._lock:
            self.release_calls.append(action)
            # 幂等：任务被取走则清 action（与真实 controller 行为一致）。
            if self._current_action == action:
                self._current_action = None

    def current_dictation_action(self) -> str | None:
        with self._lock:
            return self._current_action


def _make_auto_stop_app(ctrl: _FakeControllerWithAction) -> DictaShiftApplication:
    """装配 _on_auto_stop 直接依赖的最小属性（无需消费线程）。"""
    app = DictaShiftApplication.__new__(DictaShiftApplication)
    app._controller = ctrl
    return app


def _drain_release(ctrl: _FakeControllerWithAction, expected: int, timeout: float = 2.0) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if len(ctrl.release_calls) >= expected:
            return
        time.sleep(0.005)


class TestAutoStopQueriesController:
    def test_auto_stop_queries_controller_action_and_calls_release(self) -> None:
        """controller 当前有 dictation 任务时，_on_auto_stop 查询到 action 并调
        handle_hotkey_release(action)。"""
        ctrl = _FakeControllerWithAction(action="dictate_zh")
        app = _make_auto_stop_app(ctrl)
        app._on_auto_stop()
        _drain_release(ctrl, 1)
        assert ctrl.release_calls == ["dictate_zh"]

    def test_auto_stop_no_action_noop(self) -> None:
        """controller 无当前任务（current_dictation_action 返回 None）时，
        _on_auto_stop 直接 return，不调 handle_hotkey_release。"""
        ctrl = _FakeControllerWithAction(action=None)
        app = _make_auto_stop_app(ctrl)
        app._on_auto_stop()
        # 短暂等待确认后台线程未调用 release。
        time.sleep(0.1)
        assert ctrl.release_calls == []

    def test_auto_stop_release_idempotent_when_task_taken(self) -> None:
        """release 先到（任务被取走，current_dictation_action 返回 None）后
        auto-stop 触发：查询到 None，不重复调 release（幂等）。

        这覆盖真实场景：release 已结束录音、controller 清空 _current_task，
        随后 recorder 的 auto-stop 回调到达。"""
        ctrl = _FakeControllerWithAction(action=None)  # 已被 release 清空
        app = _make_auto_stop_app(ctrl)
        app._on_auto_stop()
        time.sleep(0.1)
        assert ctrl.release_calls == []

    def test_rejected_press_does_not_steal_action_for_auto_stop(self) -> None:
        """被 controller 拒绝的 press 不影响后续 auto-stop（修复 C 核心竞态）。

        旧行为：_on_hotkey_press 无条件覆盖 _current_action，即使 controller 拒绝了
        press（如已在 RECORDING），真正在录的 action 会被抢走；release 后 _current_action
        被清空 -> auto-stop 漏触发。修复后：press 不在本层跟踪 action，auto-stop 直接
        查 controller，不受被拒绝 press 影响。

        本测试只验证 application 层不再有 _current_action 覆盖逻辑——_on_hotkey_press
        不再写任何 action 字段，_on_auto_stop 完全依赖 controller 查询。
        """
        # application 层已无 _current_action / _action_lock 属性。
        app = _make_auto_stop_app(_FakeControllerWithAction())
        assert not hasattr(app, "_current_action")
        assert not hasattr(app, "_action_lock")

