"""全局快捷键服务（pynput 原型）。

HotkeyService 在 pynput 的 keyboard.Listener 回调线程里只做：
1. 把 pynput 的按键对象归一化为字符串键名；
2. 维护“当前已按下的归一化键集合”；
3. 对每个 binding 做边沿匹配，调用上层注入的 on_press / on_release 回调。

不变量 (AGENTS.md 5.1)：
- 回调线程内严禁阻塞工作 (录音、推理、网络、磁盘 I/O 都不在 service 内做)。
- 长按自动重复过滤：某 binding 的 on_press 只在“从不满足变为满足”时触发一次；
  重复按下事件不再触发 on_press。
- on_release：binding 中最后一个松开的构成键 (即按下集合不再满配该 binding 的
  边沿) 触发一次 on_release。
- toggle_enabled / cancel 类 binding 只触发 on_press，不触发 on_release
  (实现上直接不调用 release 回调)。

可测试性：
- Listener 通过 _create_listener 工厂方法创建，子类/测试可覆盖以注入假事件源；
- _on_key_down / _on_key_up 为纯逻辑入口，测试可直接驱动，无需真实监听。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from pynput import keyboard

from .matcher import HotkeyBinding, normalize_key_name

logger = logging.getLogger(__name__)

# 只触发 on_press、不触发 on_release 的 action 集合。
# 这些是“瞬时切换/取消/单次触发”语义，松开边沿没有业务意义。
# selection_translate：划词翻译开关；quick_lookup：快速查词唤起/关闭。
_PRESS_ONLY_ACTIONS = frozenset(
    {"toggle_enabled", "cancel", "selection_translate", "quick_lookup"}
)


def _normalize_from_pynput(key) -> str | None:
    """把 pynput 的 Key / KeyCode 对象归一化为字符串键名，无法识别返回 None。

    与 matcher.normalize_key_name 的区别：这里接收的是 pynput 对象而非字符串。
    实现细节 (以 pynput 1.x 实测为准)：
    - keyboard.Key 有 .name 属性 (如 'f8'、'ctrl_l'、'space')；
    - keyboard.KeyCode 有 .char 属性 (普通字符，可能为大写)；
    - 个别(KeyCode 没有对应字符的特殊键) .char 可能为 None，此时返回 None。

    空名称哨兵：某些按键（如多媒体键）经 pynput 给出的 .name/.char 为空字符串或
    纯空白。matcher.normalize_key_name 对空串会抛 ValueError（用于配置校验），
    但这里来自运行时按键事件，空名称应静默跳过而非走 ValueError -> ERROR 日志
    路径刷屏。因此在调用 normalize_key_name 前对空/纯空白名称提前返回 None，
    让事件被正常忽略（_on_key_down / _on_key_up 对 None 直接 return）。
    """
    if isinstance(key, keyboard.Key):
        name = key.name
        if name is None or not str(name).strip():
            logger.debug("[HotkeyService] 忽略空名称按键事件 (Key)")
            return None
        return normalize_key_name(name)
    if isinstance(key, keyboard.KeyCode):
        if key.char is not None and str(key.char).strip():
            return normalize_key_name(key.char)
        return None
    # 极少数情况 (例如 pynput 给出裸字符串)，兜底处理。
    if isinstance(key, str):
        if not key.strip():
            logger.debug("[HotkeyService] 忽略空名称按键事件 (str)")
            return None
        return normalize_key_name(key)
    return None


class HotkeyService:
    """全局快捷键服务。

    使用方式：
        svc = HotkeyService(bindings, on_press, on_release)
        svc.start()            # 启动 pynput Listener 后台线程
        svc.update_bindings(...)  # 运行时热更新，无需重启 listener
        svc.stop()
    """

    def __init__(
        self,
        bindings: list[HotkeyBinding],
        on_press: Callable[[str], None],
        on_release: Callable[[str], None],
    ) -> None:
        self._on_press_cb = on_press
        self._on_release_cb = on_release

        self._lock = threading.Lock()
        # 已按下的归一化键名集合；由 _on_key_down / _on_key_up 维护。
        self._pressed: set[str] = set()
        # 已发出按下边沿的绑定保留到物理松手，包括设置重载时移除的绑定。
        self._satisfied: dict[HotkeyBinding, None] = {}

        self._bindings: list[HotkeyBinding] = list(bindings)
        self._listener: keyboard.Listener | None = None

    # ==================== 配置 ====================

    def update_bindings(self, bindings: list[HotkeyBinding]) -> None:
        """运行时热更新绑定，不需要重启 listener。

        新配置只影响后续按下边沿。已经开始的按住动作仍由原组合键的松开结束。
        """
        with self._lock:
            self._bindings = list(bindings)

    # ==================== 生命周期 ====================

    def _create_listener(self) -> keyboard.Listener:
        """创建 pynput keyboard.Listener。

        抽成单独方法的唯一目的：允许测试通过子类覆盖来注入假事件源
        (避免真实全局监听)。生产代码使用真实 pynput Listener。
        """
        return keyboard.Listener(
            on_press=self._on_key_down,
            on_release=self._on_key_up,
        )

    def start(self) -> None:
        """启动 pynput keyboard.Listener 后台线程。重复 start 抛 RuntimeError。"""
        with self._lock:
            if self._listener is not None:
                raise RuntimeError("HotkeyService 已启动，不能重复 start")
            listener = self._create_listener()
            self._listener = listener
        listener.start()

    def stop(self) -> None:
        """停止 Listener 并等待其线程结束。未启动或已停止时安全调用 (幂等)。"""
        with self._lock:
            listener = self._listener
            self._listener = None
            self._pressed.clear()
            self._satisfied.clear()
        if listener is not None:
            listener.stop()
            listener.join(timeout=2.0)

    # ==================== 内部事件处理 (纯逻辑，可测试) ====================

    def _on_key_down(self, key) -> None:
        """pynput on_press 回调入口。只做匹配与回调，严禁阻塞。"""
        try:
            name = _normalize_from_pynput(key)
            if name is None:
                return
            with self._lock:
                self._handle_press_locked(name)
        except Exception:  # noqa: BLE001 - 回调线程内不得抛未处理异常
            logger.exception("[HotkeyService] on_press 处理异常")

    def _on_key_up(self, key) -> None:
        """pynput on_release 回调入口。只做匹配与回调，严禁阻塞。"""
        try:
            name = _normalize_from_pynput(key)
            if name is None:
                return
            with self._lock:
                self._handle_release_locked(name)
        except Exception:  # noqa: BLE001 - 回调线程内不得抛未处理异常
            logger.exception("[HotkeyService] on_release 处理异常")

    def _handle_press_locked(self, name: str) -> None:
        """在已持有 self._lock 的前提下处理一次按下事件。

        长按过滤的核心：只有当某 binding 从“不满足”变为“满足”时才触发 on_press。
        Windows 按住键不放会重复触发 on_press，但因为该 binding 已在 _satisfied 中，
        重复事件不会再次触发回调。
        """
        if name in self._pressed:
            return
        self._pressed.add(name)

        for binding in self._bindings:
            if name not in binding.keys:
                continue
            # 只有新加入的键可能让某个原先不满足的 binding 变为满足，
            # 因此仅对当前事件相关的 binding 做判定即可，避免全量扫描。
            if binding in self._satisfied:
                continue
            if binding.keys.issubset(self._pressed):
                # 改绑后的新组合不能抢走同一动作中尚未松手的录音。
                if binding.action not in _PRESS_ONLY_ACTIONS and any(
                    active.action == binding.action for active in self._satisfied
                ):
                    continue
                self._satisfied[binding] = None
                # 在锁外回调以避免回调内若取锁造成死锁；这里先收集 action，
                # 锁外逐个调用。但为简化并保持边沿原子性，回调约定“不得再次进入
                # 本 service 的任何持锁路径”，因此直接在锁内调用是安全的且更简单。
                # 注意：on_press 回调由上层保证非阻塞。
                self._fire_press(binding.action)

    def _handle_release_locked(self, name: str) -> None:
        """在已持有 self._lock 的前提下处理一次松开事件。

        on_release 边沿：binding 中最后一个松开的构成键 (即按下集合不再满配)
        触发一次 on_release。多个 binding 独立跟踪。
        """
        if name not in self._pressed:
            return
        self._pressed.discard(name)

        for binding in tuple(self._satisfied):
            if name not in binding.keys:
                continue
            # 该 binding 之前是满配状态；现在 name 被移除，必然不再满配
            # (因为 name 是其构成键之一)。
            del self._satisfied[binding]
            # toggle_enabled / cancel 只触发 on_press，不触发 on_release。
            if binding.action in _PRESS_ONLY_ACTIONS:
                continue
            self._fire_release(binding.action)

    def _fire_press(self, action: str) -> None:
        """触发 on_press 回调。调用方需自行保证不会在回调中重新持锁阻塞。"""
        try:
            self._on_press_cb(action)
        except Exception:  # noqa: BLE001 - 上层回调异常不得打死监听线程
            logger.exception("[HotkeyService] on_press 回调抛出异常 action=%s", action)

    def _fire_release(self, action: str) -> None:
        """触发 on_release 回调。"""
        try:
            self._on_release_cb(action)
        except Exception:  # noqa: BLE001 - 上层回调异常不得打死监听线程
            logger.exception("[HotkeyService] on_release 回调抛出异常 action=%s", action)
