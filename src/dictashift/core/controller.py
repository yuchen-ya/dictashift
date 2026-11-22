"""应用核心控制器：状态机 + 流水线编排。

本模块 Qt 无关（纯 Python + threading），便于脱离 UI 测试。
UI 层通过 ControllerCallbacks 接收状态/文本/错误通知，并用 queued signal
桥接到主线程。

线程模型 (AGENTS.md 5.1 / 5.2 / 11)：
- 应用层在后台队列串行交付热键和录音控制事件。
- worker 流水线在独立后台线程运行（_run_pipeline）。
- ASR 设备切换在新线程中执行，不阻塞调用方。
- controller 内部用同一把锁保护任务代次与状态迁移。
- 模型推理 / 网络 / 粘贴均在 worker 线程，绝不在 UI 主线程。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from ..asr.providers import AsrDevice
from ..output.typing import type_text_at_cursor
from ..translation import TranslationBackend, TranslationProfile
from .jobs import VoiceTask
from .state import AppState, StateMachine

logger = logging.getLogger(__name__)


# ==================== 回调定义 ====================


@dataclass
class ControllerCallbacks:
    """UI 层订阅的回调集合。

    所有回调允许在 worker 线程触发；Qt 层负责用 queued signal 桥接到主线程。

    - on_state_changed(state)：状态迁移后触发。
    - on_text_ready(text, pasted)：文本准备就绪。pasted=False 表示直输失败
      (无聚焦 caret 或 SendInput 被拦截)，需提示用户在弹窗中查看/复制文本。
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
    - 推理中取消、禁用或退出立即使任务失效，正在执行的推理自然结束。
    - 翻译配置在任务创建时冻结为快照。
    - wait_first_frame=True 时：start 成功后保持 PREPARING，等首个音频帧到达
      （_on_first_frame）或安全超时后才迁移 RECORDING；期间 release/cancel/
      toggle/stream_error 均可在 PREPARING 上正确处理（与 RECORDING 同路径或回 IDLE）。
    """

    def __init__(
        self,
        asr_service: _SupportsTranscribe,
        recorder: _SupportsRecording,
        translation_backend: TranslationBackend,
        paste_func: Callable[[str], bool] = type_text_at_cursor,
        callbacks: ControllerCallbacks | None = None,
        wait_first_frame: bool = False,
        personalize_processor=None,
    ) -> None:
        self._asr = asr_service
        self._recorder = recorder
        self._translation = translation_backend
        self._paste = paste_func
        self._callbacks = callbacks or ControllerCallbacks()
        # 个性化输出后处理器（PersonalizeProcessor；None 表示不做任何后处理）。
        # 协议：rewrite_active() -> bool；apply(text) -> str（绝不抛出）。
        self._personalize = personalize_processor

        self._sm = StateMachine(initial=AppState.IDLE)

        self._lock = threading.RLock()
        self._enabled = True
        self._shutdown = False
        # 是否等待首个音频帧再 PREPARING -> RECORDING（蓝牙设备 stream.start 立即返回，
        # 但首个回调要数百毫秒后才到）。默认 False = 旧行为（start 成功即 RECORDING）。
        self._wait_first_frame = bool(wait_first_frame)
        # 首帧安全兜底 timer：若设备一直不投递帧，超时后强制 RECORDING（宁可录也不卡死）。
        # 由 _on_first_frame（首帧到达或超时）取消。实例属性 _first_frame_timeout 便于测试。
        self._first_frame_timer: threading.Timer | None = None
        self._first_frame_timeout: float = 3.0
        # 当前正在录制的任务。松手提交音频给 worker 时移交所有权。
        self._current_task: VoiceTask | None = None
        # 开始新录音和主动失效时递增，同一任务失效后不可恢复。
        self._task_seq: int = 0

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

    def set_personalize_processor(self, processor) -> None:
        """运行时更换个性化输出后处理器（设置「应用/确定」后由装配层调用）。

        只影响后续任务；传 None 关闭全部后处理。
        """
        with self._lock:
            self._personalize = processor

    def current_dictation_action(self) -> str | None:
        task = self.current_dictation_task()
        return task.action if task is not None else None

    def current_dictation_task(self) -> VoiceTask | None:
        """取得当前录音任务，供录音回调同时记录动作和代次。"""
        with self._lock:
            return self._current_task

    # ==================== 生命周期 ====================

    def start(self, enabled: bool = True) -> None:
        """应用初始启用状态。热键监听由应用层启动。"""
        with self._lock:
            self._enabled = bool(enabled)
            self._shutdown = False
            self._goto_locked(AppState.IDLE if enabled else AppState.DISABLED)
            self._fire_enabled_changed(self._enabled)

    def shutdown(self) -> None:
        """停止接受热键，使进行中的任务失效，并释放录音流。"""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            recording = self._invalidate_task_locked()
            if self._sm.state is not AppState.DISABLED:
                self._goto_locked(AppState.IDLE)
        if recording or getattr(self._recorder, "is_recording", False):
            self._discard_recording()

    # ==================== 快捷键入口 ====================

    def handle_hotkey_press(self, action: str) -> None:
        """处理应用队列转交的启停、听写和取消事件。"""
        try:
            if action == "toggle_enabled":
                self._handle_toggle()
            elif action in _LID_BY_ACTION:
                self._handle_dictate_press(action)
            elif action == "cancel":
                self._handle_cancel()
        except Exception:  # noqa: BLE001 - hotkey 回调不得抛未处理异常
            logger.exception("[ApplicationController] handle_hotkey_press 异常 action=%s", action)
            self._report_error("处理快捷键时发生内部错误")

    def handle_hotkey_release(self, action: str, *, expected_seq: int | None = None) -> None:
        """松手或达到录音上限后提交音频。自动停止事件必须匹配原录音代次。"""
        with self._lock:
            task = self._current_task
            if task is None or task.action != action:
                return
            if expected_seq is not None and task.seq != expected_seq:
                return
            recording = self._sm.state in (AppState.PREPARING, AppState.RECORDING)
            self._current_task = None
            self._cancel_first_frame_timer_locked()

        if not recording:
            logger.warning("[ApplicationController] 松手时录音状态失步，丢弃录音")
            self._discard_recording()
            return

        try:
            audio = self._recorder.stop()
        except Exception:  # noqa: BLE001
            logger.exception("[ApplicationController] recorder.stop 异常")
            self._finish_task_with_error(task, AppState.IDLE, "结束录音时发生错误")
            return

        with self._lock:
            if task.seq != self._task_seq:
                return
            if audio is None or len(audio) == 0:
                self._goto_locked(AppState.IDLE)
                return
            if self._sm.state is AppState.PREPARING:
                self._goto_locked(AppState.RECORDING)
            self._goto_locked(AppState.TRANSCRIBING)

        threading.Thread(
            target=self._run_pipeline,
            args=(task, audio),
            name="dictashift-pipeline",
            daemon=True,
        ).start()

    def _handle_toggle(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._enabled = not self._enabled
            if self._enabled:
                recording = False
                self._goto_locked(AppState.IDLE)
            else:
                recording = self._invalidate_task_locked()
                self._goto_locked(AppState.DISABLED)
            self._fire_enabled_changed(self._enabled)
        if recording:
            self._discard_recording()

    def _handle_dictate_press(self, action: str) -> None:
        with self._lock:
            if self._shutdown or not self._enabled:
                return
            if self._sm.state is AppState.ERROR:
                self._goto_locked(AppState.IDLE)
            if self._sm.state is not AppState.IDLE:
                return
            profile = self._get_active_profile_locked() if action == "dictate_en" else None
            if action == "dictate_en" and profile is None:
                self._report_error("未配置翻译服务：请先在设置中选择翻译配置")
                return
            self._task_seq += 1
            task = VoiceTask(action=action, profile_snapshot=profile, seq=self._task_seq)
            self._current_task = task
            self._goto_locked(AppState.PREPARING)

        try:
            self._recorder.start()
        except Exception:  # noqa: BLE001
            logger.exception("[ApplicationController] recorder.start 异常")
            with self._lock:
                if self._current_task is task:
                    self._current_task = None
                    self._report_error("麦克风不可用：可能被其他应用独占或已拔出")
                    self._goto_locked(AppState.IDLE)
            return

        with self._lock:
            shutdown = self._shutdown
            if self._current_task is task and self._sm.state is AppState.PREPARING:
                if self._wait_first_frame:
                    timer = threading.Timer(
                        self._first_frame_timeout, self._on_first_frame, args=(task.seq,)
                    )
                    timer.name = "dictashift-first-frame-timeout"
                    timer.daemon = True
                    self._first_frame_timer = timer
                    timer.start()
                else:
                    self._goto_locked(AppState.RECORDING)
        if shutdown:
            self._discard_recording()

    def _on_first_frame(self, expected_seq: int | None = None) -> None:
        """首帧或该录音的等待超时使状态从准备中进入录音中。"""
        with self._lock:
            task = self._current_task
            if task is None or self._sm.state is not AppState.PREPARING:
                return
            if expected_seq is not None and task.seq != expected_seq:
                return
            self._cancel_first_frame_timer_locked()
            self._goto_locked(AppState.RECORDING)

    def _on_stream_error(self, msg: str, expected_seq: int | None = None) -> None:
        """结束发生错误的录音，忽略上一段录音迟到的错误。"""
        with self._lock:
            task = self._current_task
            if task is None or self._sm.state not in (AppState.RECORDING, AppState.PREPARING):
                return
            if expected_seq is not None and task.seq != expected_seq:
                return
            self._invalidate_task_locked()
            self._report_error("录音中断：麦克风连接异常")
            self._goto_locked(AppState.IDLE)
        self._discard_recording()

    def _handle_cancel(self) -> None:
        with self._lock:
            if self._sm.state in (AppState.IDLE, AppState.DISABLED, AppState.INSERTING):
                return
            recording = self._invalidate_task_locked()
            self._goto_locked(AppState.IDLE)
        if recording:
            self._discard_recording()

    def _invalidate_task_locked(self) -> bool:
        """使 worker 与定时器的代次失效，并交出正在录制的任务。"""
        self._task_seq += 1
        recording = self._current_task is not None
        self._current_task = None
        self._cancel_first_frame_timer_locked()
        return recording

    def _cancel_first_frame_timer_locked(self) -> None:
        if self._first_frame_timer is not None:
            self._first_frame_timer.cancel()
            self._first_frame_timer = None

    def _discard_recording(self) -> None:
        try:
            self._recorder.discard()
        except Exception:  # noqa: BLE001
            logger.exception("[ApplicationController] discard 录音异常")

    # ==================== worker 流水线 ====================

    def _run_pipeline(self, task: VoiceTask, audio: np.ndarray) -> None:
        """执行识别、翻译、改写和输入。过期 worker 不再提交任何结果。"""
        audio_seconds = len(audio) / 16000.0
        t_translate = 0.0
        try:
            with self._lock:
                if task.seq != self._task_seq:
                    return
            t0 = time.perf_counter()
            result = self._asr.transcribe(audio, lid=_LID_BY_ACTION[task.action], itn=True)
            t_transcribe = time.perf_counter() - t0
            text = getattr(result, "text", "") or ""
            if not text.strip():
                self._finish_task_with_error(task, AppState.IDLE, "未识别到语音内容，请靠近麦克风重试")
                return

            if task.profile_snapshot is not None:
                if not self._transition_task(task, AppState.TRANSLATING):
                    return
                t1 = time.perf_counter()
                text = self._translation.translate(text, task.profile_snapshot)
                t_translate = time.perf_counter() - t1

            with self._lock:
                if task.seq != self._task_seq:
                    return
                personalize = self._personalize
            if personalize is not None:
                if personalize.rewrite_active() and not self._transition_task(task, AppState.REWRITING):
                    return
                text = personalize.apply(text)

            if not self._transition_task(task, AppState.INSERTING):
                return
            # 输入是不可撤回的。与禁用/退出使用同一把锁提交，避免检查通过后再插入旧文本。
            with self._lock:
                if task.seq != self._task_seq:
                    return
                t2 = time.perf_counter()
                pasted = self._paste(text)
                t_paste = time.perf_counter() - t2
                self._fire_text_ready(text, bool(pasted))
                self._goto_locked(AppState.IDLE)
            logger.info(
                "[ApplicationController] 流水线完成: 音频 %.1fs, 转写 %.2fs, 翻译 %.2fs, 粘贴 %.2fs",
                audio_seconds, t_transcribe, t_translate, t_paste,
            )
        except Exception as e:  # noqa: BLE001
            with self._lock:
                if task.seq != self._task_seq:
                    return
                logger.exception("[ApplicationController] 流水线异常")
                self._finish_task_with_error(task, AppState.ERROR, self._safe_error_message(e))

    def _transition_task(self, task: VoiceTask, state: AppState) -> bool:
        """只允许有效任务迁移状态。任务归属和迁移由同一把锁保护。"""
        with self._lock:
            if task.seq != self._task_seq:
                return False
            self._goto_locked(state)
            return True

    def _finish_task_with_error(self, task: VoiceTask, state: AppState, message: str) -> None:
        with self._lock:
            if task.seq != self._task_seq:
                return
            self._report_error(message)
            self._goto_locked(state)

    # ==================== ASR 设备切换 ====================

    def switch_asr_device(
        self,
        device: AsrDevice,
        device_id: int = 0,
        on_done: Callable[[bool], None] | None = None,
    ) -> None:
        """在新线程中切换 ASR 设备，完成后通过 on_error 报告失败。

        不阻塞调用方（hotkey / UI 线程）。on_done(success) 在切换结束后调用
        （无论成败），供 UI 刷新"实际运行设备"显示。
        """
        def _worker() -> None:
            success = False
            try:
                self._asr.switch_device(device, device_id=device_id)
                success = True
            except Exception as e:  # noqa: BLE001
                logger.exception("[ApplicationController] ASR 设备切换失败")
                self._report_error(self._safe_error_message(e))
            if on_done is not None:
                try:
                    on_done(success)
                except Exception:  # noqa: BLE001
                    logger.exception("[ApplicationController] switch_asr_device on_done 回调异常")

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

    def _goto_locked(self, target: AppState) -> None:
        """合法迁移并触发状态回调。

        若当前已在 target，视为幂等无操作（避免 self-transition 抛异常）。
        """
        if self._sm.state == target:
            return
        self._sm.transition(target)
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
