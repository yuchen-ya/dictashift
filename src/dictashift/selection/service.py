"""划词监听服务 (pynput 鼠标监听 -> FIFO 队列 -> 单消费线程取词)。

骨架与 hotkeys/service.py 一致：
- pynput mouse.Listener 回调线程内只做判定与入队，严禁阻塞
  (取词涉及 SendInput 与剪贴板等待，全部在消费线程执行)。
- 单消费线程从 FIFO 队列取事件，调用注入的 capture_func，
  把结果交给注入的 on_result 回调。
- set_auto_trigger(False) 时鼠标划选事件在 _on_click 入口即被丢弃
  （listener 与消费线程保持运行），供设置页「划选文字后自动弹出翻译」
  开关热更新；on_press_activity 通知不受影响（任意左键按下都会触发）。
- on_press_activity（可选）：任意左键按下时调用一次，供装配层立即收起
  旧弹窗（缩短弹窗拦截后续划选起点按压的时间窗）。
  线程约束：在 pynput 监听线程内同步调用，必须非阻塞；装配层传入的是
  Qt signal emit（QueuedConnection 投递到主线程），满足约束。
- 不 import PySide6：纯逻辑服务，Qt 桥接 (信号投递到主线程) 留给装配层。

可测试性：
- Listener 通过 _create_listener 工厂创建，测试可注入假事件源；
- _on_click 为纯逻辑入口，测试可直接驱动，无需真实全局监听。
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from pynput import mouse

from .capture import CaptureResult, capture_selection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectionEvent:
    """一次划选取词触发（左键按下 -> 拖拽超阈值 -> 松开）。"""

    x: int | None = None  # 划选松开位置 (供后续弹窗定位)
    y: int | None = None


class SelectionService:
    """划词监听服务。

    使用方式：
        svc = SelectionService(on_result=handle_result)
        svc.start()            # 启动 mouse.Listener + 消费线程
        svc.stop()
    """

    def __init__(
        self,
        on_result: Callable[[CaptureResult], None],
        capture_func: Callable[[], CaptureResult] | None = None,
        drag_threshold_px: float = 10.0,
        cooldown_s: float = 0.8,
        on_press_activity: Callable[[], None] | None = None,
    ) -> None:
        self._on_result_cb = on_result
        self._capture = capture_func or capture_selection
        self._drag_threshold = float(drag_threshold_px)
        self._cooldown = float(cooldown_s)
        # 任意左键按下活动回调（在 pynput 监听线程调用，必须非阻塞）。
        self._on_press_activity = on_press_activity

        self._lock = threading.Lock()
        # 左键按下位置与时间 (用于拖拽距离判定)。
        self._press_pos: tuple[int, int] | None = None
        self._press_time = 0.0
        # 上次触发时间 (冷却期判定)。
        self._last_trigger = 0.0
        # 划选自动触发开关 (set_auto_trigger 热更新)；False 时 _on_click 直接丢弃。
        self._auto_trigger = True

        self._queue: queue.Queue[SelectionEvent | object] = queue.Queue()
        self._sentinel = object()  # 停止哨兵
        self._listener: mouse.Listener | None = None
        self._consumer: threading.Thread | None = None

    # ==================== 生命周期 ====================

    def _create_listener(self) -> mouse.Listener:
        """创建 pynput mouse.Listener。抽成工厂方法供测试注入假事件源。"""
        return mouse.Listener(on_click=self._on_click)

    def start(self) -> None:
        """启动消费线程与 mouse.Listener。重复 start 抛 RuntimeError。"""
        with self._lock:
            if self._listener is not None or self._consumer is not None:
                raise RuntimeError("SelectionService 已启动，不能重复 start")
            consumer = threading.Thread(
                target=self._consume,
                name="SelectionServiceConsumer",
                daemon=True,
            )
            self._consumer = consumer
            listener = self._create_listener()
            self._listener = listener
        # 先启动消费线程再启动 listener，保证事件入队时已有人消费。
        consumer.start()
        listener.start()

    def stop(self) -> None:
        """停止 Listener 与消费线程。未启动或已停止时安全调用 (幂等)。"""
        with self._lock:
            listener = self._listener
            consumer = self._consumer
            self._listener = None
            self._consumer = None
            self._press_pos = None
        if listener is not None:
            listener.stop()
            listener.join(timeout=2.0)
        if consumer is not None:
            self._queue.put(self._sentinel)
            consumer.join(timeout=2.0)

    # ==================== 触发入口 ====================

    def set_auto_trigger(self, enabled: bool) -> None:
        """运行时开关划选自动触发（设置「应用/确定」后由装配层调用）。

        False 时 _on_click 入口即丢弃鼠标划选事件（不记录按下位置、不入队）；
        listener 与消费线程保持运行，on_press_activity 通知不受影响。
        关闭瞬间丢弃可能残留的按下位置，避免重新开启后误配对旧按下。
        """
        with self._lock:
            self._auto_trigger = bool(enabled)
            if not self._auto_trigger:
                self._press_pos = None

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        """pynput on_click 回调入口。只做判定与入队，严禁阻塞。"""
        try:
            if button != mouse.Button.left:
                return
            if pressed and self._on_press_activity is not None:
                # 任意左键按下都通知（含 auto_trigger 关闭时）：供装配层立即
                # 收起旧弹窗。本回调在 pynput 监听线程执行，on_press_activity
                # 必须非阻塞（装配层传入 Qt signal emit，QueuedConnection 投递）。
                try:
                    self._on_press_activity()
                except Exception:  # noqa: BLE001 - 活动通知失败不得影响划选判定
                    logger.exception("[SelectionService] on_press_activity 回调异常")
            now = time.monotonic()
            with self._lock:
                if not self._auto_trigger:
                    return
                if pressed:
                    self._press_pos = (x, y)
                    self._press_time = now
                    return
                if self._press_pos is None:
                    return
                press_x, press_y = self._press_pos
                self._press_pos = None
                distance = math.hypot(x - press_x, y - press_y)
                if distance <= self._drag_threshold:
                    return
                if now - self._last_trigger < self._cooldown:
                    return
                self._last_trigger = now
            self._queue.put(SelectionEvent(x=x, y=y))
        except Exception:  # noqa: BLE001 - 回调线程内不得抛未处理异常
            logger.exception("[SelectionService] on_click 处理异常")

    # ==================== 消费线程 ====================

    def _consume(self) -> None:
        """单消费线程：取事件 -> capture_func -> on_result。哨兵值退出。"""
        while True:
            event = self._queue.get()
            try:
                if event is self._sentinel:
                    return
                try:
                    result = self._capture()
                except Exception:  # noqa: BLE001 - 取词异常不得打死消费线程
                    logger.exception("[SelectionService] capture_func 抛出异常")
                    continue
                try:
                    self._on_result_cb(result)
                except Exception:  # noqa: BLE001 - 上层回调异常不得打死消费线程
                    logger.exception("[SelectionService] on_result 回调抛出异常")
            finally:
                self._queue.task_done()
