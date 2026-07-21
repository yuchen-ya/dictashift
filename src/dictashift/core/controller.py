"""应用核心控制器：状态机 + 流水线编排。

本模块 Qt 无关（纯 Python + threading），便于脱离 UI 测试。
UI 层通过 ControllerCallbacks 接收状态/文本/错误通知，并用 queued signal
桥接到主线程。

线程模型 (AGENTS.md 5.1 / 5.2 / 11)：
- hotkey 回调来自 pynput 监听线程（handle_hotkey_press / handle_hotkey_release）。
- worker 流水线在独立后台线程运行（_run_pipeline）。
- ASR 设备切换在新线程中执行，不阻塞调用方。
- controller 内部用 threading.Lock 保护状态迁移与中断标记。
- 模型推理 / 网络 / 粘贴均在 worker 线程，绝不在 UI 主线程。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from ..asr.providers import AsrDevice
from ..output.win32_paste import paste_text
from ..translation import TranslationBackend, TranslationProfile
from .jobs import VoiceTask
from .state import AppState, InvalidTransition, StateMachine

logger = logging.getLogger(__name__)


# ==================== 回调定义 ====================


@dataclass
class ControllerCallbacks:
    """UI 层订阅的回调集合。

    所有回调允许在 worker 线程触发；Qt 层负责用 queued signal 桥接到主线程。

    - on_state_changed(state)：状态迁移后触发。
    - on_text_ready(text, pasted)：文本准备就绪。pasted=False 表示 SendInput
      注入失败但剪贴板已保留，需提示用户手动 Ctrl+V。
    - on_error(message)：出错；消息已脱敏，不含 api_key 等敏感信息。
    - on_enabled_changed(enabled)：启用/禁用状态变化。
    """

    on_state_changed: Callable[[AppState], None] | None = None
    on_text_ready: Callable[[str, bool], None] | None = None
    on_error: Callable[[str], None] | None = None
    on_enabled_changed: Callable[[bool], None] | None = None


# ==================== 服务协议（用于类型注解，依赖注入） ====================


class _SupportsTranscribe(Protocol):
    def transcribe(self, audio: np.ndarray, lid: str = ..., itn: bool = ...):  # pragma: no cover - 协议
        ...

    def switch_device(self, device: AsrDevice, device_id: int = 0) -> None:  # pragma: no cover
        ...


class _SupportsRecording(Protocol):
    def start(self) -> None:  # pragma: no cover - 协议
        ...

    def stop(self) -> np.ndarray:  # pragma: no cover
        ...

    def discard(self) -> None:  # pragma: no cover
        ...


# ASR lid 取值：中文用 zh，英文用 auto（避免英文被强制中文化）
_LID_BY_ACTION = {"dictate_zh": "zh", "dictate_en": "auto"}


class ApplicationController:
    """核心控制器。

    依赖全部注入，便于用 fake asr / recorder / backend / paste_func 测试。

    状态不变量：
    - 处理期间默认不接受新的语音任务（只有 IDLE 且 enabled 时才开始录音）。
    - toggle_enabled 在任何状态可响应。
    - 录音中禁用 -> discard 丢弃。
    - 推理中禁用 -> 设置 _disabled_mid_task 标记，任务自然结束但不得粘贴。
    - 翻译配置在任务创建时冻结为快照。
    """

    def __init__(
        self,
        asr_service: _SupportsTranscribe,
        recorder: _SupportsRecording,
        translation_backend: TranslationBackend,
        paste_func: Callable[[str], bool] = paste_text,
        callbacks: ControllerCallbacks | None = None,
    ) -> None:
        self._asr = asr_service
        self._recorder = recorder
        self._translation = translation_backend
        self._paste = paste_func
        self._callbacks = callbacks or ControllerCallbacks()

        self._sm = StateMachine(initial=AppState.IDLE)

        self._lock = threading.Lock()
        self._enabled = True
        self._shutdown = False
        # 当前进行中的任务（按下快捷键时创建）。仅 IDLE->RECORDING 时非空。
        self._current_task: VoiceTask | None = None
        # 中断标记：worker 在关键检查点读取，决定是否继续粘贴。
        self._cancel_requested = False
        # 推理中禁用标记：toggle 在推理阶段触发时置位，任务结束后不得粘贴。
        self._disabled_mid_task = False

        # 运行时翻译配置（可由 set_translation_profiles 切换）。
        self._translation_profiles: list[TranslationProfile] = []
        self._active_profile_name: str = ""

    # ==================== 属性 ====================

    @property
    def state(self) -> AppState:
        return self._sm.state

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def get_active_profile(self) -> TranslationProfile | None:
        """返回当前活动翻译档案；无则返回 None。"""
        with self._lock:
            for p in self._translation_profiles:
                if p.name == self._active_profile_name:
                    return p
            return None

    def set_translation_profiles(
        self, profiles: list[TranslationProfile], active_name: str
    ) -> None:
        """运行时切换翻译配置。

        只影响后续任务（快照机制保证已开始任务不受影响）。
        """
        with self._lock:
            self._translation_profiles = list(profiles)
            self._active_profile_name = active_name

    # ==================== 生命周期 ====================

    def start(self, enabled: bool = True) -> None:
        """应用初始启用状态。

        - enabled=True：状态保持/迁移到 IDLE。
        - enabled=False：迁移到 DISABLED。

        注意：hotkey bindings 的注册由 application 层完成，controller 只暴露
        handle_hotkey_press / handle_hotkey_release。
        """
        with self._lock:
            self._enabled = bool(enabled)
            self._shutdown = False
        if enabled:
            self._goto(AppState.IDLE)
        else:
            self._goto(AppState.DISABLED)
        self._fire_enabled_changed(bool(enabled))

    def shutdown(self) -> None:
        """关闭：录音中则 discard，标记 shutdown。"""
        with self._lock:
            self._shutdown = True
        # 录音中丢弃；推理中不打断（让 worker 自然结束，但禁用粘贴由 shutdown 标记保证）。
        if self._recorder is not None and self._is_recording_safe():
            try:
                self._recorder.discard()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] shutdown discard 录音异常")
            with self._lock:
                self._current_task = None
            try:
                self._sm.transition(AppState.IDLE)
                self._fire_state_changed()
            except InvalidTransition:
                self._force_state(AppState.IDLE)

    def _is_recording_safe(self) -> bool:
        """判断是否正在录音，兼容 fake recorder 可能没有 is_recording 属性。"""
        is_rec = getattr(self._recorder, "is_recording", None)
        if callable(is_rec):
            try:
                return bool(is_rec())
            except Exception:  # noqa: BLE001
                return False
        # 没有 is_recording 属性时，按状态机判断。
        return self._sm.state == AppState.RECORDING

    # ==================== 快捷键入口 ====================

    def handle_hotkey_press(self, action: str) -> None:
        """快捷键按下处理（pynput 监听线程调用）。

        - toggle_enabled：任何状态响应。
        - dictate_zh / dictate_en：仅 IDLE 且 enabled 时开始录音。
        - cancel：RECORDING -> discard -> IDLE；推理中设置取消标记。
        """
        try:
            if action == "toggle_enabled":
                self._handle_toggle()
            elif action in ("dictate_zh", "dictate_en"):
                self._handle_dictate_press(action)
            elif action == "cancel":
                self._handle_cancel()
            # 未知 action 静默忽略。
        except Exception:  # noqa: BLE001 - hotkey 回调不得抛未处理异常
            logger.exception("[ApplicationController] handle_hotkey_press 异常 action=%s", action)
            self._report_error("处理快捷键时发生内部错误")

    def handle_hotkey_release(self, action: str) -> None:
        """快捷键松开处理（pynput 监听线程调用）。

        仅 dictate_zh / dictate_en 且当前 RECORDING 且 action 匹配当前任务时
        结束录音并启动 worker。
        """
        try:
            if action not in ("dictate_zh", "dictate_en"):
                return
            audio: np.ndarray
            task: VoiceTask
            with self._lock:
                if self._current_task is None or self._current_task.action != action:
                    return
                if self._sm.state != AppState.RECORDING:
                    return
                # 取出任务，清空当前指针；stop 在锁外执行（recorder.stop 可能阻塞）。
                task = self._current_task
                self._current_task = None
            # 结束录音：在锁外调用，避免与 recorder 内部锁死锁。
            try:
                audio = self._recorder.stop()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] recorder.stop 异常")
                self._report_error("结束录音时发生错误")
                self._force_state(AppState.IDLE)
                return

            # 空录音直接回 IDLE，不进入流水线。
            if audio is None or len(audio) == 0:
                logger.info("[ApplicationController] 空录音，回 IDLE")
                self._goto(AppState.IDLE)
                return

            # 迁移到 TRANSCRIBING 并启动 worker。
            self._goto(AppState.TRANSCRIBING)
            # 线程名便于调试。
            t = threading.Thread(
                target=self._run_pipeline,
                args=(task, audio),
                name="dictashift-pipeline",
                daemon=True,
            )
            t.start()
        except Exception:  # noqa: BLE001
            logger.exception(
                "[ApplicationController] handle_hotkey_release 异常 action=%s", action
            )
            self._report_error("处理快捷键松开时发生内部错误")

    # ---------- press 分支 ----------

    def _handle_toggle(self) -> None:
        with self._lock:
            new_enabled = not self._enabled
            current_state = self._sm.state

        if new_enabled:
            # 启用：恢复到 IDLE。任何状态都可恢复（DISABLED -> IDLE 合法；
            # 其他状态由 force 兜底，但正常路径下只有 DISABLED 时才会被禁用）。
            with self._lock:
                self._enabled = True
                self._cancel_requested = False
                self._disabled_mid_task = False
            if current_state == AppState.DISABLED:
                self._goto(AppState.IDLE)
            else:
                # 极端情况：禁用标记与状态不一致；force 到 IDLE 保持一致。
                self._force_state(AppState.IDLE)
            self._fire_enabled_changed(True)
        else:
            # 禁用：录音中 discard；推理中设置标记（任务自然结束但不粘贴）。
            with self._lock:
                self._enabled = False
                if current_state in (AppState.TRANSCRIBING, AppState.TRANSLATING):
                    # 推理中：不打断，标记任务结束后不得粘贴。
                    self._disabled_mid_task = True
                elif current_state == AppState.RECORDING:
                    # 录音中：稍后 discard 并迁移。
                    pass
            if current_state == AppState.RECORDING:
                try:
                    self._recorder.discard()
                except Exception:  # noqa: BLE001
                    logger.exception("[ApplicationController] 禁用时 discard 异常")
                with self._lock:
                    self._current_task = None
                # RECORDING -> DISABLED 合法。
                self._goto(AppState.DISABLED)
            elif current_state in (AppState.TRANSCRIBING, AppState.TRANSLATING, AppState.INSERTING):
                # 推理/插入中：让任务自然结束。状态先不迁移（worker 结束时
                # 检查 _disabled_mid_task 决定是否 force 到 DISABLED）。
                # 但为了对外反映禁用语义，这里仍然把状态迁到 DISABLED。
                # 注意：worker 内部后续的状态迁移会因此变得非法，
                # 故 worker 在结束时用 force 兜底。这里用 force 立即反映禁用。
                with self._lock:
                    self._enabled = False
                self._force_state(AppState.DISABLED)
            elif current_state == AppState.IDLE:
                # IDLE -> DISABLED 合法。
                self._goto(AppState.DISABLED)
            elif current_state == AppState.ERROR:
                self._force_state(AppState.DISABLED)
            elif current_state == AppState.DISABLED:
                # 已禁用，幂等。
                pass
            self._fire_enabled_changed(False)

    def _handle_dictate_press(self, action: str) -> None:
        with self._lock:
            if not self._enabled:
                return
            if self._sm.state != AppState.IDLE:
                # 处理期间不接受新任务。
                return
            # 创建任务并冻结翻译快照。
            snapshot: TranslationProfile | None = None
            if action == "dictate_en":
                snap = self._get_active_profile_locked()
                if snap is not None:
                    snapshot = snap
                else:
                    # 无可用翻译档案，dictate_en 无法进行；静默忽略并报错。
                    logger.warning(
                        "[ApplicationController] dictate_en 缺少活动翻译档案，忽略"
                    )
                    return
            self._current_task = VoiceTask(action=action, profile_snapshot=snapshot)
            # 清除上一轮的中断标记。
            self._cancel_requested = False
            self._disabled_mid_task = False

        # 开始录音（锁外，recorder.start 可能涉及 PortAudio）。
        try:
            self._recorder.start()
        except Exception:  # noqa: BLE001
            logger.exception("[ApplicationController] recorder.start 异常")
            with self._lock:
                self._current_task = None
            self._report_error("无法开始录音（音频设备不可用）")
            return
        self._goto(AppState.RECORDING)

    def _handle_cancel(self) -> None:
        with self._lock:
            current = self._sm.state
        if current == AppState.RECORDING:
            try:
                self._recorder.discard()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] cancel discard 异常")
            with self._lock:
                self._current_task = None
            # RECORDING -> IDLE 合法。
            self._goto(AppState.IDLE)
        elif current in (AppState.TRANSCRIBING, AppState.TRANSLATING):
            # 推理中取消：设置取消标记，任务自然结束但不粘贴。
            # 设计选择：立即把状态 force 到 IDLE，让 UI 即时反馈取消，
            # worker 结束时检查标记跳过粘贴。worker 内的状态迁移用 try/except
            # 兜底，避免因已迁到 IDLE 而抛 InvalidTransition。
            with self._lock:
                self._cancel_requested = True
            self._force_state(AppState.IDLE)
        # 其他状态（IDLE/DISABLED/INSERTING/ERROR）忽略。

    # ==================== worker 流水线 ====================

    def _run_pipeline(self, task: VoiceTask, audio: np.ndarray) -> None:
        """后台流水线：transcribe -> (translate) -> paste -> IDLE。

        任何异常 -> ERROR + on_error。取消/禁用标记在关键检查点生效。
        """
        try:
            # ---- TRANSCRIBING ----
            lid = _LID_BY_ACTION.get(task.action, "auto")
            result = self._asr.transcribe(audio, lid=lid, itn=True)
            text = getattr(result, "text", "") or ""

            if self._should_suppress_output():
                self._finish_suppressed()
                return

            if not text.strip():
                # 无识别文本，直接回 IDLE。
                self._goto_safe(AppState.IDLE)
                return

            # ---- 翻译（仅 dictate_en）----
            if task.action == "dictate_en" and task.profile_snapshot is not None:
                self._goto_safe(AppState.TRANSLATING)
                if self._should_suppress_output():
                    self._finish_suppressed()
                    return
                translated = self._translation.translate(text, task.profile_snapshot)
                text = translated

            if self._should_suppress_output():
                self._finish_suppressed()
                return

            # ---- INSERTING ----
            self._goto_safe(AppState.INSERTING)
            if self._should_suppress_output():
                self._finish_suppressed()
                return

            pasted = self._paste(text)
            # 粘贴成功与否都通过 on_text_ready 通知；pasted=False 表示剪贴板已保留。
            self._fire_text_ready(text, bool(pasted))
            self._goto_safe(AppState.IDLE)
        except Exception as e:  # noqa: BLE001
            logger.exception("[ApplicationController] 流水线异常")
            self._report_error(self._safe_error_message(e))
            self._force_state(AppState.ERROR)

    def _should_suppress_output(self) -> bool:
        """检查是否应抑制输出（取消或推理中禁用）。"""
        with self._lock:
            return self._cancel_requested or self._disabled_mid_task

    def _finish_suppressed(self) -> None:
        """任务被取消/禁用抑制后的收尾：回 IDLE 或 DISABLED。"""
        with self._lock:
            disabled = not self._enabled
        # 状态可能已被 force 到 IDLE（cancel）或 DISABLED（toggle）。
        # 这里用 force 兜底到正确终态。
        target = AppState.DISABLED if disabled else AppState.IDLE
        self._force_state(target)

    def _goto_safe(self, target: AppState) -> None:
        """尝试合法迁移；失败（例如被 cancel force 到 IDLE 后）则 force 到 target。"""
        try:
            self._sm.transition(target)
            self._fire_state_changed()
        except InvalidTransition:
            self._force_state(target)

    # ==================== ASR 设备切换 ====================

    def switch_asr_device(self, device: AsrDevice, device_id: int = 0) -> None:
        """在新线程中切换 ASR 设备，完成后通过 on_error 报告失败。

        不阻塞调用方（hotkey / UI 线程）。
        """
        def _worker() -> None:
            try:
                self._asr.switch_device(device, device_id=device_id)
            except Exception as e:  # noqa: BLE001
                logger.exception("[ApplicationController] ASR 设备切换失败")
                self._report_error(self._safe_error_message(e))

        t = threading.Thread(
            target=_worker, name="dictashift-asr-switch", daemon=True
        )
        t.start()

    # ==================== 内部工具 ====================

    def _get_active_profile_locked(self) -> TranslationProfile | None:
        """在已持有 self._lock 的前提下返回活动 profile。"""
        for p in self._translation_profiles:
            if p.name == self._active_profile_name:
                return p
        return None

    def _goto(self, target: AppState) -> None:
        """合法迁移并触发状态回调。

        若当前已在 target，视为幂等无操作（避免 self-transition 抛异常）。
        """
        if self._sm.state == target:
            return
        self._sm.transition(target)
        self._fire_state_changed()

    def _force_state(self, target: AppState) -> None:
        self._sm.force(target)
        self._fire_state_changed()

    def _fire_state_changed(self) -> None:
        cb = self._callbacks.on_state_changed
        if cb is not None:
            try:
                cb(self._sm.state)
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] on_state_changed 回调异常")

    def _fire_text_ready(self, text: str, pasted: bool) -> None:
        cb = self._callbacks.on_text_ready
        if cb is not None:
            try:
                cb(text, pasted)
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] on_text_ready 回调异常")

    def _fire_enabled_changed(self, enabled: bool) -> None:
        cb = self._callbacks.on_enabled_changed
        if cb is not None:
            try:
                cb(enabled)
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] on_enabled_changed 回调异常")

    def _report_error(self, message: str) -> None:
        logger.warning("[ApplicationController] 上报错误: %s", message)
        cb = self._callbacks.on_error
        if cb is not None:
            try:
                cb(message)
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] on_error 回调异常")

    @staticmethod
    def _safe_error_message(e: BaseException) -> str:
        """构造脱敏错误消息：不含 api_key 等敏感信息。

        异常类型名 + str(e) 中过滤常见敏感模式；保守起见只取类型名与截断消息。
        """
        msg = str(e)
        # 过滤可能泄露的长串（api_key 一般不会出现在异常消息中，但保守截断）。
        if len(msg) > 200:
            msg = msg[:200] + "..."
        return f"{type(e).__name__}: {msg}"
