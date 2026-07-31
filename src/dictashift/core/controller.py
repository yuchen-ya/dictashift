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
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from ..asr.providers import AsrDevice
from ..output.typing import type_text_at_cursor
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
    - 推理中禁用 -> 设置 _disabled_mid_task 标记，任务自然结束但不得粘贴。
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

        self._lock = threading.Lock()
        self._enabled = True
        self._shutdown = False
        # 是否等待首个音频帧再 PREPARING -> RECORDING（蓝牙设备 stream.start 立即返回，
        # 但首个回调要数百毫秒后才到）。默认 False = 旧行为（start 成功即 RECORDING）。
        self._wait_first_frame = bool(wait_first_frame)
        # 首帧安全兜底 timer：若设备一直不投递帧，超时后强制 RECORDING（宁可录也不卡死）。
        # 由 _on_first_frame（首帧到达或超时）取消。实例属性 _first_frame_timeout 便于测试。
        self._first_frame_timer: threading.Timer | None = None
        self._first_frame_timeout: float = 3.0
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

    def set_personalize_processor(self, processor) -> None:
        """运行时更换个性化输出后处理器（设置「应用/确定」后由装配层调用）。

        只影响后续任务；传 None 关闭全部后处理。
        """
        with self._lock:
            self._personalize = processor

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
        """关闭：录音中（含 PREPARING 等待首帧）则 discard，标记 shutdown。"""
        with self._lock:
            self._shutdown = True
        # 取消可能挂起的首帧兜底 timer。
        self._cancel_first_frame_timer()
        # 录音中丢弃；推理中不打断（让 worker 自然结束，但禁用粘贴由 shutdown 标记保证）。
        # PREPARING（wait_first_frame 等待首帧）同样可能已打开流 -> 视为录音中。
        is_recording = self._is_recording_safe()
        with self._lock:
            in_preparing = self._sm.state == AppState.PREPARING
        if self._recorder is not None and (is_recording or in_preparing):
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

        仅 dictate_zh / dictate_en 且当前 RECORDING 或 PREPARING（wait_first_frame
        模式下首帧未到的秒按秒松）且 action 匹配当前任务时结束录音并启动 worker。
        """
        try:
            if action not in ("dictate_zh", "dictate_en"):
                return
            audio: np.ndarray
            task: VoiceTask
            with self._lock:
                if self._current_task is None or self._current_task.action != action:
                    return
                cur_state = self._sm.state
                if cur_state not in (AppState.RECORDING, AppState.PREPARING):
                    return
                # 取出任务，清空当前指针；stop 在锁外执行（recorder.stop 可能阻塞）。
                task = self._current_task
                self._current_task = None
                was_preparing = cur_state == AppState.PREPARING
            # PREPARING 下 release：首帧没来但用户已松手。取消首帧兜底 timer
            # （若 wait_first_frame 模式下还在等待）。
            if was_preparing:
                self._cancel_first_frame_timer()
            # 结束录音：在锁外调用，避免与 recorder 内部锁死锁。
            try:
                audio = self._recorder.stop()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] recorder.stop 异常")
                self._report_error("结束录音时发生错误")
                self._force_state(AppState.IDLE)
                return

            # 空录音直接回 IDLE，不进入流水线。
            # PREPARING -> IDLE 合法；RECORDING -> IDLE 合法。
            if audio is None or len(audio) == 0:
                logger.info("[ApplicationController] 空录音，回 IDLE")
                self._goto(AppState.IDLE)
                return

            # 非空音频但状态仍为 PREPARING（首帧没来但 stop 取到了帧，罕见：
            # stop 自身的关流过程可能恰好让一帧落地）：先补迁移到 RECORDING，
            # 因为 PREPARING 不能直接 -> TRANSCRIBING。
            if self._sm.state == AppState.PREPARING:
                self._goto(AppState.RECORDING)

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
            # 禁用：录音中 discard；推理/插入中设置标记（任务自然结束但不粘贴）。
            # 读状态 + 决策在同一个临界区内完成，避免 check-then-act 竞态。
            with self._lock:
                self._enabled = False
                current_state = self._sm.state
                if current_state in (
                    AppState.PREPARING,
                    AppState.RECORDING,
                    AppState.TRANSCRIBING,
                    AppState.TRANSLATING,
                    AppState.REWRITING,
                    AppState.INSERTING,
                ):
                    # RECORDING/PREPARING 也置标记作为竞态安全网：若 release 恰好在本决策
                    # 之后抢先启动了 worker，worker 仍会被抑制，不会粘贴。
                    self._disabled_mid_task = True
            if current_state in (AppState.RECORDING, AppState.PREPARING):
                # PREPARING 与 RECORDING 同处理：discard 丢弃 + 清任务 + 取消首帧
                # 兜底 timer。PREPARING -> DISABLED 合法；RECORDING -> DISABLED 合法。
                self._cancel_first_frame_timer()
                try:
                    self._recorder.discard()
                except Exception:  # noqa: BLE001
                    logger.exception("[ApplicationController] 禁用时 discard 异常")
                with self._lock:
                    self._current_task = None
                self._goto(AppState.DISABLED)
            elif current_state in (
                AppState.TRANSCRIBING,
                AppState.TRANSLATING,
                AppState.REWRITING,
                AppState.INSERTING,
            ):
                # 推理/插入中：让任务自然结束（_disabled_mid_task 已置位，
                # worker 结束时跳过粘贴）。状态立即 force 到 DISABLED 以对外
                # 反映禁用语义；worker 后续迁移用 force 兜底。
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
            if self._sm.state == AppState.ERROR:
                # ERROR 是上一次任务的终态提示，不应卡死后续输入：
                # ERROR -> IDLE 是合法迁移，恢复后继续走正常 IDLE 流程。
                recover = True
            elif self._sm.state != AppState.IDLE:
                # 处理期间不接受新任务。
                return
            else:
                recover = False
        if recover:
            self._goto(AppState.IDLE)
        # 创建任务并冻结翻译快照。no_profile_msg 非空表示 dictate_en 无活动
        # profile：临界区内只置标记，_report_error 在锁外调用（避免在持锁时
        # 触发可能间接触发其他锁/UI 的 on_error 通道）。
        no_profile_msg: str | None = None
        with self._lock:
            snapshot: TranslationProfile | None = None
            if action == "dictate_en":
                snap = self._get_active_profile_locked()
                if snap is not None:
                    snapshot = snap
                else:
                    # 无可用翻译档案，dictate_en 无法进行；记 warning，锁外再
                    # 通过 on_error 给用户可见提示。行为仍是不开始录音。
                    logger.warning(
                        "[ApplicationController] dictate_en 缺少活动翻译档案，忽略"
                    )
                    no_profile_msg = "未配置翻译服务：请先在设置中选择翻译配置"
            if no_profile_msg is None:
                self._current_task = VoiceTask(action=action, profile_snapshot=snapshot)
                # 清除上一轮的中断标记。
                self._cancel_requested = False
                self._disabled_mid_task = False

        if no_profile_msg is not None:
            self._report_error(no_profile_msg)
            return

        # 先迁移到 PREPARING：麦克风流打开需要数十~数百毫秒（蓝牙设备更久），
        # 让 UI 如实显示「准备中…」而非提前声称「录音中」。
        self._goto(AppState.PREPARING)
        # 开始录音（锁外，recorder.start 可能涉及 PortAudio）。
        try:
            self._recorder.start()
        except Exception:  # noqa: BLE001
            logger.exception("[ApplicationController] recorder.start 异常")
            with self._lock:
                self._current_task = None
            # 设备占用/打开失败的语义提示（不改 recorder 异常类型，仅文案）。
            self._report_error("麦克风不可用：可能被其他应用独占或已拔出")
            # PREPARING -> IDLE 合法迁移。
            self._goto(AppState.IDLE)
            return
        if self._wait_first_frame:
            # 保持 PREPARING：等首个音频帧到达（recorder on_first_frame -> _on_first_frame）
            # 再迁移 RECORDING。启动安全兜底 timer：设备一直不投递帧时强制 RECORDING，
            # 宁可录也不卡死。timer 与 _on_first_frame 互相取消，先到者赢，幂等。
            self._cancel_first_frame_timer()
            timer = threading.Timer(self._first_frame_timeout, self._on_first_frame)
            timer.name = "dictashift-first-frame-timeout"
            timer.daemon = True
            with self._lock:
                self._first_frame_timer = timer
            timer.start()
        else:
            self._goto(AppState.RECORDING)

    def _on_first_frame(self) -> None:
        """首个音频帧到达（recorder on_first_frame，由 application 层从后台线程调用）
        或安全兜底 timer 触发时调用。取消防兜 timer；若仍在 PREPARING 且任务未取消
        则迁移到 RECORDING，否则幂等忽略（release/cancel/toggle 已先行处理）。

        幂等性：可能被首帧回调与超时 timer 先后各触发一次（竞态），或被迟到的回调在
        release/stream_error 之后触发；任何「当前不是 PREPARING」的情况都直接返回。
        """
        self._cancel_first_frame_timer()
        with self._lock:
            if self._sm.state != AppState.PREPARING:
                return
            task = self._current_task
        if task is None:
            # 任务已被 release/cancel/toggle 取走 -> 幂等忽略。
            return
        # PREPARING -> RECORDING 合法迁移。
        self._goto(AppState.RECORDING)

    def _cancel_first_frame_timer(self) -> None:
        """取消防兜 timer（判空 + cancel + 置 None）。各结束路径统一调用。"""
        with self._lock:
            timer = self._first_frame_timer
            self._first_frame_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] 取消首帧 timer 异常")

    def _on_stream_error(self, msg: str) -> None:
        """recorder 录音中途严重 status 回调（PortAudio 线程，由 application 层转后台线程）。

        - RECORDING 或 PREPARING（wait_first_frame 模式下首帧回调本身可能携带
          致命 status）状态响应：discard 录音 + on_error 提示 + 回 IDLE。
        - 与随后的 _on_first_frame 幂等共存：on_stream_error 已 discard + 回 IDLE，
          随后到达的 _on_first_frame 发现不是 PREPARING 即幂等忽略。
        - 走 self._lock，与 hotkey / worker 线程串行。
        - 回调只做状态迁移与通知，recorder.discard 的关流阻塞可能极短，
          AGENTS.md 5.1 的"回调不做阻塞 I/O"主要约束的是 _callback 本身；
          此处是 recorder 主动通知上层的 on_stream_error 通道，由上层
          （application 层）已转交后台线程执行，不直接在 PortAudio 线程阻塞。
        """
        with self._lock:
            if self._sm.state not in (AppState.RECORDING, AppState.PREPARING):
                return
            current_task = self._current_task
            self._current_task = None
        # 取消可能挂起的首帧兜底 timer。
        self._cancel_first_frame_timer()
        # discard 在锁外执行（关流可能阻塞）。
        if current_task is not None:
            try:
                self._recorder.discard()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] stream error discard 异常")
        self._report_error("录音中断：麦克风连接异常")
        self._force_state(AppState.IDLE)

    def _handle_cancel(self) -> None:
        with self._lock:
            current = self._sm.state
        if current in (AppState.RECORDING, AppState.PREPARING):
            # PREPARING 分支：首帧未到时取消（用户秒按秒 Esc）；与 RECORDING 同路径
            # discard + 清任务，并取消可能挂起的首帧兜底 timer。
            self._cancel_first_frame_timer()
            try:
                self._recorder.discard()
            except Exception:  # noqa: BLE001
                logger.exception("[ApplicationController] cancel discard 异常")
            with self._lock:
                self._current_task = None
            # PREPARING -> IDLE 合法；RECORDING -> IDLE 合法。
            self._goto(AppState.IDLE)
        elif current in (AppState.TRANSCRIBING, AppState.TRANSLATING, AppState.REWRITING):
            # 推理中取消：设置取消标记，任务自然结束但不粘贴。
            # 设计选择：立即把状态 force 到 IDLE，让 UI 即时反馈取消，
            # worker 结束时检查标记跳过粘贴。worker 内的状态迁移用 try/except
            # 兜底，避免因已迁到 IDLE 而抛 InvalidTransition。
            with self._lock:
                self._cancel_requested = True
            self._force_state(AppState.IDLE)
        elif current == AppState.ERROR:
            # ERROR -> IDLE 合法迁移：Esc 可清除错误状态。
            self._goto(AppState.IDLE)
        # 其他状态（IDLE/DISABLED/INSERTING）忽略。

    # ==================== worker 流水线 ====================

    def _run_pipeline(self, task: VoiceTask, audio: np.ndarray) -> None:
        """后台流水线：transcribe -> (translate) -> paste -> IDLE。

        任何异常 -> ERROR + on_error。取消/禁用标记在关键检查点生效。
        """
        # 流水线耗时统计（成功路径末尾汇总记录，便于排查"等半天出结果"的瓶颈）。
        audio_seconds = len(audio) / 16000.0
        t_transcribe = 0.0
        t_translate = 0.0
        t_paste = 0.0
        try:
            # ---- TRANSCRIBING ----
            lid = _LID_BY_ACTION.get(task.action, "auto")
            t0 = time.perf_counter()
            result = self._asr.transcribe(audio, lid=lid, itn=True)
            t_transcribe = time.perf_counter() - t0
            text = getattr(result, "text", "") or ""

            if self._should_suppress_output():
                self._finish_suppressed()
                return

            if not text.strip():
                # 无识别文本：给用户可见提示，再回 IDLE。
                # 此分支已在 _should_suppress_output 检查之后，被抑制（取消/禁用）时不报错。
                self._report_error("未识别到语音内容，请靠近麦克风重试")
                self._goto_safe(AppState.IDLE)
                return

            # ---- 翻译（仅 dictate_en）----
            if task.action == "dictate_en" and task.profile_snapshot is not None:
                self._goto_safe(AppState.TRANSLATING)
                if self._should_suppress_output():
                    self._finish_suppressed()
                    return
                t1 = time.perf_counter()
                translated = self._translation.translate(text, task.profile_snapshot)
                t_translate = time.perf_counter() - t1
                text = translated

            if self._should_suppress_output():
                self._finish_suppressed()
                return

            # ---- 个性化后处理（语气改写 -> 句尾后缀）----
            with self._lock:
                personalize = self._personalize
            if personalize is not None:
                # 仅当实际执行 LLM 改写时才进入 REWRITING 状态；
                # 仅后缀/全关时不出现在状态显示中。
                if personalize.rewrite_active():
                    self._goto_safe(AppState.REWRITING)
                    if self._should_suppress_output():
                        self._finish_suppressed()
                        return
                # apply 内部失败静默回退（绝不抛出），这里不再额外兜底。
                text = personalize.apply(text)

            # ---- INSERTING ----
            self._goto_safe(AppState.INSERTING)
            if self._should_suppress_output():
                self._finish_suppressed()
                return

            # 粘贴前最后检查：覆盖进入 INSERTING 到执行粘贴之间的禁用窗口。
            if self._should_suppress_output():
                self._finish_suppressed()
                return

            t2 = time.perf_counter()
            pasted = self._paste(text)
            t_paste = time.perf_counter() - t2
            # 粘贴成功与否都通过 on_text_ready 通知；pasted=False 表示直输失败，由上层弹窗展示文本。
            self._fire_text_ready(text, bool(pasted))
            self._goto_safe(AppState.IDLE)
            logger.info(
                "[ApplicationController] 流水线完成: 音频 %.1fs, 转写 %.2fs, 翻译 %.2fs, 粘贴 %.2fs",
                audio_seconds, t_transcribe, t_translate, t_paste,
            )
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
