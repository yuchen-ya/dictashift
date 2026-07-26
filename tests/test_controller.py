"""ApplicationController 单元测试（fake asr / recorder / backend / paste_func）。

测试目标 (AGENTS.md 5.1 / 5.2 / 5.4 / 13.1)：
- 完整流水线 dictate_zh：press -> release -> worker -> on_text_ready(text, True) -> IDLE。
- dictate_en 翻译：用任务创建时的 profile 快照（运行时切换不影响进行中任务）。
- 处理期间拒绝新任务（RECORDING 中再 press -> recorder.start 只调一次）。
- 录音中禁用：discard + DISABLED + 无粘贴。
- 推理中禁用：阻塞 transcribe -> toggle -> 释放 -> 不粘贴、无 on_text_ready。
- 推理中取消（Esc）：不粘贴、状态最终 IDLE。
- 禁用状态按 dictate：不录音。
- DISABLED 时 toggle 可恢复 -> IDLE + enabled=True。
- 空录音：直接回 IDLE，asr.transcribe 未被调用。
- 粘贴失败：paste_func 返回 False -> on_text_ready(text, False)。
- worker 异常：asr.transcribe raise -> ERROR + on_error。
- dictate_en 无活动 profile 时不录音。

worker 是后台线程，测试用 threading.Event 等待回调（超时 5s）。
本文件不依赖任何外部资源。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pytest

from dictashift.core.controller import ApplicationController, ControllerCallbacks
from dictashift.core.state import AppState
from dictashift.translation import TranslationProfile


# ==================== Fakes ====================


@dataclass
class FakeTranscriptionResult:
    """模拟 asr.sensevoice_onnx.TranscriptionResult 的最小接口（含 .text）。"""

    text: str = ""


class FakeAsrService:
    """记录调用、可控制 transcribe 阻塞与异常的 fake ASR 服务。"""

    def __init__(
        self,
        text: str = "你好世界",
        raise_on_transcribe: Exception | None = None,
        block_event: threading.Event | None = None,
    ) -> None:
        self._text = text
        self._raise = raise_on_transcribe
        self._block = block_event
        self.transcribe_calls: list[dict] = []
        self.switch_device_calls: list[tuple] = []

    def transcribe(self, audio: np.ndarray, lid: str = "auto", itn: bool = True):
        self.transcribe_calls.append(
            {"audio_len": len(audio) if audio is not None else 0, "lid": lid, "itn": itn}
        )
        if self._block is not None:
            self._block.wait(timeout=5.0)
        if self._raise is not None:
            raise self._raise
        return FakeTranscriptionResult(text=self._text)

    def switch_device(self, device, device_id: int = 0) -> None:
        self.switch_device_calls.append((device, device_id))


class FakeRecorder:
    """记录 start/stop/discard、可控 stop 返回内容的 fake 录音器。"""

    def __init__(self, stop_audio: np.ndarray | None = None) -> None:
        self._stop_audio = (
            stop_audio if stop_audio is not None else np.ones(1600, dtype=np.float32)
        )
        self.start_calls = 0
        self.stop_calls = 0
        self.discard_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> np.ndarray:
        self.stop_calls += 1
        return self._stop_audio

    def discard(self) -> None:
        self.discard_calls += 1


class FakeTranslationBackend:
    """记录 translate 调用与所用 profile 的 fake 翻译后端。"""

    def __init__(self, result: str = "hello world") -> None:
        self._result = result
        self.translate_calls: list[tuple[str, TranslationProfile]] = []

    def translate_stream(self, text: str, profile: TranslationProfile):
        # 实现生成器接口，但控制器走 translate() 便捷方法（拼接）。
        yield from self.translate(text, profile)

    def translate(self, text: str, profile: TranslationProfile) -> str:
        self.translate_calls.append((text, profile))
        return self._result


@dataclass
class RecordedCallbacks:
    """收集控制器回调的简单容器。"""

    states: list[AppState] = field(default_factory=list)
    texts: list[tuple[str, bool]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    enabled: list[bool] = field(default_factory=list)
    # 用于跨线程等待的事件
    state_event: threading.Event = field(default_factory=threading.Event)
    text_event: threading.Event = field(default_factory=threading.Event)
    error_event: threading.Event = field(default_factory=threading.Event)

    def as_callbacks(self) -> ControllerCallbacks:
        def _on_state(s: AppState) -> None:
            self.states.append(s)
            self.state_event.set()

        def _on_text(text: str, pasted: bool) -> None:
            self.texts.append((text, pasted))
            self.text_event.set()

        def _on_error(msg: str) -> None:
            self.errors.append(msg)
            self.error_event.set()

        def _on_enabled(en: bool) -> None:
            self.enabled.append(en)

        return ControllerCallbacks(
            on_state_changed=_on_state,
            on_text_ready=_on_text,
            on_error=_on_error,
            on_enabled_changed=_on_enabled,
        )


def _make_controller(
    asr: FakeAsrService | None = None,
    recorder: FakeRecorder | None = None,
    backend: FakeTranslationBackend | None = None,
    paste_func: Callable[[str], bool] | None = None,
    callbacks: RecordedCallbacks | None = None,
    wait_first_frame: bool = False,
) -> tuple[ApplicationController, RecordedCallbacks, FakeAsrService, FakeRecorder, FakeTranslationBackend]:
    asr = asr or FakeAsrService()
    recorder = recorder or FakeRecorder()
    backend = backend or FakeTranslationBackend()
    cb = callbacks or RecordedCallbacks()
    ctrl = ApplicationController(
        asr_service=asr,
        recorder=recorder,
        translation_backend=backend,
        paste_func=paste_func if paste_func is not None else (lambda t: True),
        callbacks=cb.as_callbacks(),
        wait_first_frame=wait_first_frame,
    )
    ctrl.start(enabled=True)
    return ctrl, cb, asr, recorder, backend


def _wait(event: threading.Event, timeout: float = 5.0) -> bool:
    """等待事件，避免挂死。"""
    return event.wait(timeout=timeout)


# ==================== 完整流水线 dictate_zh ====================


def test_dictate_zh_full_pipeline() -> None:
    """press -> release -> worker -> on_text_ready(text, True) -> IDLE。"""
    ctrl, cb, asr, recorder, _ = _make_controller()

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING
    assert recorder.start_calls == 1

    cb.state_event.clear()
    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")

    # 等待 worker 完成（最终回 IDLE，并产出文本）
    assert _wait(cb.text_event)
    assert _wait(cb.state_event)
    # 最终状态应为 IDLE
    _wait_for_state(cb, AppState.IDLE)

    assert recorder.stop_calls == 1
    assert len(asr.transcribe_calls) == 1
    # lid 应为 zh
    assert asr.transcribe_calls[0]["lid"] == "zh"
    assert cb.texts == [("你好世界", True)]
    assert ctrl.state == AppState.IDLE


def test_dictate_zh_full_pipeline_logs_timings(caplog) -> None:
    """成功路径末尾输出一行流水线耗时汇总日志（音频/转写/翻译/粘贴）。"""
    import logging

    ctrl, cb, asr, recorder, _ = _make_controller()
    with caplog.at_level(logging.INFO, logger="dictashift.core.controller"):
        ctrl.handle_hotkey_press("dictate_zh")
        cb.state_event.clear()
        cb.text_event.clear()
        ctrl.handle_hotkey_release("dictate_zh")
        assert _wait(cb.text_event)
        _wait_for_state(cb, AppState.IDLE)

    # 汇总日志一行，含关键字「流水线完成」与各项耗时标签。
    summary = [r.message for r in caplog.records if "流水线完成" in r.message]
    assert len(summary) == 1, f"期望恰好一条汇总日志，实际 {summary}"
    msg = summary[0]
    assert "音频" in msg
    assert "转写" in msg
    assert "翻译" in msg
    assert "粘贴" in msg
    # dictate_zh 无翻译分支，翻译耗时应为 0.00。
    assert "翻译 0.00s" in msg


def _wait_for_state(cb: RecordedCallbacks, target: AppState, timeout: float = 5.0) -> None:
    """轮询回调状态列表，直到出现 target 或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if target in cb.states:
            return
        cb.state_event.clear()
        cb.state_event.wait(timeout=0.05)
    assert target in cb.states, f"未在 {timeout}s 内观测到状态 {target}；states={cb.states}"


# ==================== dictate_en 翻译 + 快照语义 ====================


_PROFILE_OLD = TranslationProfile(
    name="old", base_url="http://old/v1", model="m-old", api_key=None, temperature=0.3
)
_PROFILE_NEW = TranslationProfile(
    name="new", base_url="http://new/v1", model="m-new", api_key=None, temperature=0.3
)


def test_dictate_en_uses_snapshot_profile() -> None:
    """任务进行中切换 profile，worker 仍收到任务创建时的快照。"""
    ctrl, cb, asr, recorder, backend = _make_controller()
    ctrl.set_translation_profiles([_PROFILE_OLD], active_name="old")

    ctrl.handle_hotkey_press("dictate_en")
    assert ctrl.state == AppState.RECORDING
    assert recorder.start_calls == 1

    # 任务进行中切换到新 profile（任务已创建，快照已冻结）
    ctrl.set_translation_profiles([_PROFILE_NEW], active_name="new")

    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_en")

    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)

    # 翻译被调用，且用的是旧 profile 快照
    assert len(backend.translate_calls) == 1
    _text, used_profile = backend.translate_calls[0]
    assert used_profile.name == "old"
    assert used_profile.base_url == "http://old/v1"
    assert used_profile.model == "m-old"
    # lid 应为 auto
    assert asr.transcribe_calls[0]["lid"] == "auto"
    # 译文被作为最终文本输出
    assert cb.texts == [("hello world", True)]


# ==================== 处理期间拒绝新任务 ====================


def test_new_task_rejected_while_recording() -> None:
    """RECORDING 中再 press dictate_en -> recorder.start 只被调用一次。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    ctrl.set_translation_profiles([_PROFILE_OLD], active_name="old")

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING
    assert recorder.start_calls == 1

    # 在 RECORDING 中再按 dictate_en，应被拒绝
    ctrl.handle_hotkey_press("dictate_en")
    assert recorder.start_calls == 1  # 仍未再次 start
    assert ctrl.state == AppState.RECORDING

    # 清理：松开原 zh 任务
    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)


# ==================== 录音中禁用 ====================


def test_disable_while_recording_discards_no_paste() -> None:
    """RECORDING 时 toggle -> discard + DISABLED + 后续无粘贴。"""
    paste_calls: list[str] = []
    ctrl, cb, asr, recorder, _ = _make_controller(
        paste_func=lambda t: paste_calls.append(t) or True
    )

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING

    ctrl.handle_hotkey_press("toggle_enabled")
    assert recorder.discard_calls == 1
    assert ctrl.state == AppState.DISABLED
    assert ctrl.enabled is False

    # 等待一小段时间确认没有 worker / paste
    time.sleep(0.2)
    assert paste_calls == []
    assert cb.texts == []
    assert asr.transcribe_calls == []


# ==================== 推理中禁用 ====================


def test_disable_while_transcribing_no_paste() -> None:
    """推理中禁用：transcribe 阻塞 -> toggle -> 释放 -> 不粘贴、无 on_text_ready。"""
    block = threading.Event()
    paste_calls: list[str] = []
    asr = FakeAsrService(text="some text", block_event=block)
    ctrl, cb, _asr, recorder, _ = _make_controller(
        asr=asr, paste_func=lambda t: paste_calls.append(t) or True
    )

    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")
    # 等 worker 进入 TRANSCRIBING
    _wait_for_state(cb, AppState.TRANSCRIBING)

    # 推理中禁用
    ctrl.handle_hotkey_press("toggle_enabled")
    assert ctrl.enabled is False

    # 释放 transcribe，让 worker 继续
    block.set()

    # 等待 worker 结束
    _wait_for_state(cb, AppState.DISABLED, timeout=5.0)
    time.sleep(0.2)

    # 关键断言：不粘贴、无 on_text_ready
    assert paste_calls == []
    assert cb.texts == []


def test_disable_while_inserting_no_paste() -> None:
    """INSERTING 窗口禁用：进入 INSERTING 后、执行粘贴前 toggle -> 不粘贴。"""
    paste_calls: list[str] = []
    asr = FakeAsrService(text="some text")
    ctrl, cb, _asr, recorder, _ = _make_controller(
        asr=asr, paste_func=lambda t: paste_calls.append(t) or True
    )

    # 在进入 INSERTING 的瞬间（worker 线程内）触发禁用，
    # 精确命中「进入 INSERTING -> 执行粘贴」之间的窗口。
    original_goto_safe = ctrl._goto_safe

    def goto_safe_with_toggle(target: AppState) -> None:
        original_goto_safe(target)
        if target == AppState.INSERTING:
            ctrl.handle_hotkey_press("toggle_enabled")

    ctrl._goto_safe = goto_safe_with_toggle

    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")

    _wait_for_state(cb, AppState.DISABLED, timeout=5.0)
    time.sleep(0.2)

    assert ctrl.enabled is False
    assert paste_calls == []
    assert cb.texts == []


# ==================== 推理中取消（Esc）====================


def test_cancel_while_transcribing_no_paste_idle() -> None:
    """推理中 cancel -> 不粘贴、状态最终 IDLE。"""
    block = threading.Event()
    paste_calls: list[str] = []
    asr = FakeAsrService(text="some text", block_event=block)
    ctrl, cb, _asr, recorder, _ = _make_controller(
        asr=asr, paste_func=lambda t: paste_calls.append(t) or True
    )

    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")
    _wait_for_state(cb, AppState.TRANSCRIBING)

    ctrl.handle_hotkey_press("cancel")

    # 释放 transcribe
    block.set()

    # 最终状态应为 IDLE
    _wait_for_state(cb, AppState.IDLE, timeout=5.0)
    time.sleep(0.2)

    assert paste_calls == []
    assert cb.texts == []


# ==================== 禁用状态按 dictate ====================


def test_dictate_in_disabled_does_not_record() -> None:
    """DISABLED 时 press dictate_zh -> recorder.start 未被调用。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    ctrl.handle_hotkey_press("toggle_enabled")
    assert ctrl.state == AppState.DISABLED
    assert ctrl.enabled is False

    ctrl.handle_hotkey_press("dictate_zh")
    assert recorder.start_calls == 0
    assert ctrl.state == AppState.DISABLED


# ==================== DISABLED 时 toggle 恢复 ====================


def test_toggle_recovers_from_disabled() -> None:
    """DISABLED -> toggle -> IDLE 且 enabled=True。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    ctrl.handle_hotkey_press("toggle_enabled")
    assert ctrl.state == AppState.DISABLED
    assert ctrl.enabled is False

    ctrl.handle_hotkey_press("toggle_enabled")
    assert ctrl.state == AppState.IDLE
    assert ctrl.enabled is True


# ==================== 空录音 ====================


def test_empty_recording_returns_to_idle_no_transcribe() -> None:
    """recorder.stop 返回空 np 数组 -> 直接回 IDLE，asr.transcribe 未被调用。"""
    empty_recorder = FakeRecorder(stop_audio=np.empty(0, dtype=np.float32))
    ctrl, cb, asr, recorder, _ = _make_controller(recorder=empty_recorder)

    cb.state_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING

    ctrl.handle_hotkey_release("dictate_zh")
    # 空录音应快速回 IDLE，不进入流水线
    _wait(cb.state_event)
    assert ctrl.state == AppState.IDLE
    assert asr.transcribe_calls == []
    assert cb.texts == []


# ==================== 粘贴失败 ====================


def test_paste_failure_reports_not_pasted() -> None:
    """paste_func 返回 False -> on_text_ready(text, False)。"""
    ctrl, cb, asr, recorder, _ = _make_controller(paste_func=lambda t: False)

    cb.text_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")

    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)
    assert cb.texts == [("你好世界", False)]


# ==================== ASR 返回空识别文本 ====================


def test_empty_asr_text_reports_error_and_returns_idle() -> None:
    """ASR transcribe 返回空文本 -> on_error 收到「未识别到语音内容」提示，
    状态最终回 IDLE（不进入翻译/粘贴），且不触发 on_text_ready。
    """
    paste_calls: list[str] = []
    asr = FakeAsrService(text="   ")  # 仅空白字符，strip() 后为空
    ctrl, cb, _asr, recorder, _ = _make_controller(
        asr=asr, paste_func=lambda t: paste_calls.append(t) or True
    )

    cb.error_event.clear()
    cb.text_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")

    # 等待 on_error 触发
    assert _wait(cb.error_event)
    _wait_for_state(cb, AppState.IDLE)

    # 错误提示含「未识别到语音内容」语义
    assert len(cb.errors) == 1
    assert "未识别到语音内容" in cb.errors[0]
    # 不应进入翻译/粘贴路径
    assert cb.texts == []
    assert paste_calls == []
    assert ctrl.state == AppState.IDLE


def test_empty_asr_text_suppressed_does_not_report_error() -> None:
    """对照：ASR 返回空文本，但任务在 _should_suppress_output 检查点已被取消
    抑制时，走 _finish_suppressed 分支，不走空文本的 on_error。

    实现要点：让 transcribe 阻塞到取消信号发出后才放行，确保 worker 走到
    _should_suppress_output 检查点时 _cancel_requested 已为 True。
    """
    block = threading.Event()
    asr = FakeAsrService(text="", block_event=block)
    ctrl, cb, _asr, recorder, _ = _make_controller(asr=asr)

    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")
    _wait_for_state(cb, AppState.TRANSCRIBING)

    # 推理中取消（worker 仍阻塞在 transcribe）
    ctrl.handle_hotkey_press("cancel")
    # 放行 transcribe，使 worker 走到 _should_suppress_output 检查点（已被取消）
    block.set()

    _wait_for_state(cb, AppState.IDLE, timeout=5.0)
    time.sleep(0.2)
    # 被抑制路径不应触发空文本的 on_error
    assert cb.errors == []


# ==================== worker 异常 ====================


def test_worker_exception_goes_to_error() -> None:
    """asr.transcribe raise -> 状态 ERROR + on_error 被调用。"""
    asr = FakeAsrService(raise_on_transcribe=RuntimeError("boom"))
    ctrl, cb, _asr, recorder, _ = _make_controller(asr=asr)

    cb.error_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")

    assert _wait(cb.error_event)
    _wait_for_state(cb, AppState.ERROR)
    assert ctrl.state == AppState.ERROR
    assert len(cb.errors) == 1
    # 错误消息脱敏：不含敏感长串（这里只是类型名+消息）
    assert "boom" in cb.errors[0] or "RuntimeError" in cb.errors[0]


def test_dictate_press_recovers_from_error() -> None:
    """ERROR 状态下按 dictate：先恢复 IDLE 再正常开始录音（不卡死）。"""
    asr = FakeAsrService(raise_on_transcribe=RuntimeError("boom"))
    ctrl, cb, _asr, recorder, _ = _make_controller(asr=asr)

    cb.error_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.error_event)
    _wait_for_state(cb, AppState.ERROR)

    # ERROR 后再次按 dictate：应恢复并开始新一次录音
    starts_before = recorder.start_calls
    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING
    assert recorder.start_calls == starts_before + 1
    ctrl.handle_hotkey_release("dictate_zh")


def test_cancel_recovers_from_error() -> None:
    """ERROR 状态下按 Esc：恢复 IDLE。"""
    asr = FakeAsrService(raise_on_transcribe=RuntimeError("boom"))
    ctrl, cb, _asr, recorder, _ = _make_controller(asr=asr)

    cb.error_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.error_event)
    _wait_for_state(cb, AppState.ERROR)

    ctrl.handle_hotkey_press("cancel")
    assert ctrl.state == AppState.IDLE


# ==================== dictate_en 无活动 profile ====================


def test_dictate_en_without_active_profile_does_not_record() -> None:
    """dictate_en 无活动 profile 时 press -> 不录音。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    # 不设置任何 profile，active 为空

    ctrl.handle_hotkey_press("dictate_en")
    assert recorder.start_calls == 0
    assert ctrl.state == AppState.IDLE


def test_dictate_en_without_active_profile_reports_error() -> None:
    """dictate_en 无活动 profile 时通过 on_error 给用户可见提示，且不录音。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    cb.error_event.clear()

    ctrl.handle_hotkey_press("dictate_en")
    assert _wait(cb.error_event)
    assert recorder.start_calls == 0
    assert ctrl.state == AppState.IDLE
    assert len(cb.errors) == 1
    assert "翻译" in cb.errors[0] or "设置" in cb.errors[0]


def test_dictate_en_with_profile_starts_recording_and_no_error() -> None:
    """对照测试：有活动 profile 时不报错，正常录音。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    ctrl.set_translation_profiles([_PROFILE_OLD], active_name="old")
    cb.error_event.clear()

    ctrl.handle_hotkey_press("dictate_en")
    assert recorder.start_calls == 1
    assert ctrl.state == AppState.RECORDING
    assert cb.errors == []
    # 清理
    rec_audio = np.ones(1600, dtype=np.float32)
    recorder._stop_audio = rec_audio  # type: ignore[attr-defined]
    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_en")
    assert _wait(cb.text_event)


# ==================== 录音中 stream error ====================


def test_stream_error_while_recording_discards_and_reports() -> None:
    """RECORDING 中触发 controller._on_stream_error -> discard + on_error + IDLE。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING

    cb.error_event.clear()
    ctrl._on_stream_error("output overflow")

    # 等待 on_error
    assert _wait(cb.error_event)
    # discard 被调用一次
    assert recorder.discard_calls == 1
    # 状态回 IDLE
    assert ctrl.state == AppState.IDLE
    # 错误消息含"录音中断"语义
    assert len(cb.errors) == 1
    assert "录音中断" in cb.errors[0] or "麦克风" in cb.errors[0]


def test_stream_error_ignored_when_not_recording() -> None:
    """非 RECORDING 状态下 _on_stream_error 不触发状态迁移/discard/error。"""
    ctrl, cb, asr, recorder, _ = _make_controller()
    # 当前 IDLE
    assert ctrl.state == AppState.IDLE
    cb.error_event.clear()

    ctrl._on_stream_error("output overflow")
    # 短暂等待确认无事件
    assert not cb.error_event.wait(timeout=0.2)
    assert recorder.discard_calls == 0
    assert ctrl.state == AppState.IDLE
    assert cb.errors == []


def test_stream_error_after_release_does_not_clobber_pipeline() -> None:
    """release 已结束录音（进入 TRANSCRIBING）后 stream error 不应误打断流水线。"""
    paste_calls: list[str] = []
    ctrl, cb, asr, recorder, _ = _make_controller(
        paste_func=lambda t: paste_calls.append(t) or True
    )
    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_release("dictate_zh")
    # 等 worker 进入 TRANSCRIBING
    _wait_for_state(cb, AppState.TRANSCRIBING)

    # 此时 stream error 回调进来（状态非 RECORDING）-> 应被忽略
    ctrl._on_stream_error("output overflow")

    _wait_for_state(cb, AppState.IDLE)
    time.sleep(0.2)
    # 流水线照常粘贴
    assert len(paste_calls) == 1
    assert ctrl.state == AppState.IDLE


# ==================== controller 默认 paste_func 语义 ====================


def test_default_paste_func_is_type_text_at_cursor() -> None:
    """ApplicationController.__init__ 的默认 paste_func 为
    dictashift.output.typing.type_text_at_cursor。

    断言语义（由主会话确定）：controller 在调用方不显式传 paste_func 时，
    默认走 type_text_at_cursor 直输路径，而非旧的剪贴板粘贴。
    用 inspect.signature 检查默认参数值（定义时绑定，最准确）。
    """
    import inspect

    from dictashift.core import controller as controller_mod
    from dictashift.output.typing import type_text_at_cursor

    sig = inspect.signature(controller_mod.ApplicationController.__init__)
    param = sig.parameters["paste_func"]
    # 默认值必须是 typing 模块里的 type_text_at_cursor（同一函数对象）
    assert param.default is type_text_at_cursor


def test_controller_instance_default_paste_is_typing_function() -> None:
    """不传 paste_func 构造 controller，实例 _paste 绑定为 type_text_at_cursor。

    补充 inspect 断言：确保运行期实例确实持有了默认 paste 函数对象，
    而非 None 或其它占位。
    """
    from dictashift.output.typing import type_text_at_cursor

    asr = FakeAsrService()
    recorder = FakeRecorder()
    backend = FakeTranslationBackend()
    # 不传 paste_func
    ctrl = ApplicationController(
        asr_service=asr,
        recorder=recorder,
        translation_backend=backend,
        callbacks=RecordedCallbacks().as_callbacks(),
    )
    assert ctrl._paste is type_text_at_cursor


# ==================== PREPARING 过渡状态 ====================


class _BlockingStartRecorder(FakeRecorder):
    """start 阻塞直到放行，用于观察 PREPARING 过渡状态。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered_start = threading.Event()
        self.release_start = threading.Event()

    def start(self) -> None:
        self.start_calls += 1
        self.entered_start.set()
        self.release_start.wait(timeout=5.0)


def test_press_enters_preparing_until_recorder_start_completes() -> None:
    """按下听写键后、recorder.start 完成前状态必须是 PREPARING（而非提前声称
    RECORDING）；start 完成后才迁移到 RECORDING。"""
    recorder = _BlockingStartRecorder()
    ctrl, cb, _, _, _ = _make_controller(recorder=recorder)

    t = threading.Thread(
        target=ctrl.handle_hotkey_press, args=("dictate_zh",), daemon=True
    )
    t.start()
    assert recorder.entered_start.wait(timeout=2.0)
    # start 阻塞中：状态应为 PREPARING，且 on_state_changed 已通知过
    assert ctrl.state == AppState.PREPARING
    assert AppState.PREPARING in cb.states

    recorder.release_start.set()
    t.join(timeout=2.0)
    assert ctrl.state == AppState.RECORDING

    # 收尾：release 结束任务，回 IDLE
    ctrl.handle_hotkey_release("dictate_zh")
    _wait_for_state(cb, AppState.IDLE)


def test_recorder_start_failure_returns_idle_with_error() -> None:
    """recorder.start 抛异常 -> on_error 提示麦克风不可用，状态 PREPARING -> IDLE，
    状态机不卡死，可再次发起听写（再次失败仍回 IDLE）。"""

    class _FailingStartRecorder(FakeRecorder):
        def start(self) -> None:
            self.start_calls += 1
            raise RuntimeError("mock open failure")

    ctrl, cb, _, recorder, _ = _make_controller(recorder=_FailingStartRecorder())
    cb.error_event.clear()
    ctrl.handle_hotkey_press("dictate_zh")

    assert _wait(cb.error_event)
    assert any("麦克风不可用" in m for m in cb.errors)
    assert ctrl.state == AppState.IDLE

    # 再次按下：流程可重入（同步失败 -> 仍回 IDLE，错误再报一次）
    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.IDLE
    assert recorder.start_calls == 2


# ==================== wait_first_frame：首帧驱动的 PREPARING -> RECORDING ====================


def test_wait_first_frame_press_stays_preparing() -> None:
    """wait_first_frame=True：press -> start 成功后保持 PREPARING（不是 RECORDING）。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert recorder.start_calls == 1
    assert ctrl.state == AppState.PREPARING
    # 防兜 timer 已挂起
    assert ctrl._first_frame_timer is not None


def test_wait_first_frame_first_frame_moves_to_recording() -> None:
    """wait_first_frame=True：press -> PREPARING；手动 _on_first_frame -> RECORDING；
    之后 release 走正常流水线。"""
    ctrl, cb, asr, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    # 首帧到达
    ctrl._on_first_frame()
    assert ctrl.state == AppState.RECORDING
    # 防兜 timer 已被取消
    assert ctrl._first_frame_timer is None

    # release 走正常流水线
    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)
    assert recorder.stop_calls == 1
    assert len(asr.transcribe_calls) == 1
    assert ctrl.state == AppState.IDLE


def test_wait_first_frame_safety_timeout_moves_to_recording() -> None:
    """安全兜底：press 后不手动触发，等 timer（_first_frame_timeout 设为 0.1）-> 自动 RECORDING。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)
    ctrl._first_frame_timeout = 0.1

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    # 等待 timer 触发（容差给到 2s）
    _wait_for_state(cb, AppState.RECORDING, timeout=2.0)
    assert ctrl.state == AppState.RECORDING
    assert ctrl._first_frame_timer is None

    # 收尾
    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)


def test_wait_first_frame_release_during_preparing_returns_idle() -> None:
    """PREPARING 中 release（首帧未到）：recorder.stop 被调、空录音 -> IDLE，无 transcribe。"""
    empty_recorder = FakeRecorder(stop_audio=np.empty(0, dtype=np.float32))
    ctrl, cb, asr, recorder, _ = _make_controller(
        recorder=empty_recorder, wait_first_frame=True
    )

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    cb.state_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    # 空录音 -> 回 IDLE，不进流水线
    assert _wait(cb.state_event)
    assert ctrl.state == AppState.IDLE
    assert recorder.stop_calls == 1
    assert asr.transcribe_calls == []
    # 防兜 timer 已取消
    assert ctrl._first_frame_timer is None


def test_wait_first_frame_release_during_preparing_with_audio_runs_pipeline() -> None:
    """PREPARING 中 release，但 stop 恰好取到帧（罕见）：补迁 RECORDING 后走流水线。"""
    ctrl, cb, asr, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    # 取到了默认非空音频 -> 走流水线
    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)
    assert recorder.stop_calls == 1
    assert len(asr.transcribe_calls) == 1
    assert ctrl.state == AppState.IDLE


def test_wait_first_frame_cancel_during_preparing_returns_idle() -> None:
    """PREPARING 中 cancel：discard 被调 -> IDLE。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    ctrl.handle_hotkey_press("cancel")
    assert recorder.discard_calls == 1
    assert ctrl.state == AppState.IDLE
    assert ctrl._first_frame_timer is None


def test_wait_first_frame_toggle_disable_during_preparing() -> None:
    """PREPARING 中 toggle 禁用：discard 被调 -> DISABLED；再 toggle 恢复 IDLE。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    ctrl.handle_hotkey_press("toggle_enabled")
    assert recorder.discard_calls == 1
    assert ctrl.state == AppState.DISABLED
    assert ctrl.enabled is False
    assert ctrl._first_frame_timer is None

    # 再 toggle 恢复
    ctrl.handle_hotkey_press("toggle_enabled")
    assert ctrl.state == AppState.IDLE
    assert ctrl.enabled is True


def test_wait_first_frame_stream_error_during_preparing() -> None:
    """PREPARING 中 _on_stream_error：discard + on_error + 回 IDLE；随后 _on_first_frame 幂等无副作用。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    cb.error_event.clear()
    ctrl._on_stream_error("output overflow")
    assert _wait(cb.error_event)
    assert recorder.discard_calls == 1
    assert ctrl.state == AppState.IDLE
    assert ctrl._first_frame_timer is None

    # 随后的 _on_first_frame 幂等：状态非 PREPARING，无副作用
    cb.error_event.clear()
    ctrl._on_first_frame()
    assert ctrl.state == AppState.IDLE
    assert not cb.error_event.wait(timeout=0.2)
    assert recorder.discard_calls == 1  # 未再 discard


def test_wait_first_frame_first_frame_idempotent_after_release() -> None:
    """release 在 PREPARING 取走任务后，迟到的 _on_first_frame 幂等忽略。"""
    empty_recorder = FakeRecorder(stop_audio=np.empty(0, dtype=np.float32))
    ctrl, cb, _, recorder, _ = _make_controller(
        recorder=empty_recorder, wait_first_frame=True
    )

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING

    cb.state_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.state_event)
    assert ctrl.state == AppState.IDLE

    # 迟到的首帧回调：幂等忽略
    ctrl._on_first_frame()
    assert ctrl.state == AppState.IDLE


def test_wait_first_frame_first_frame_idempotent_after_toggle_disable() -> None:
    """toggle 禁用 PREPARING 后，迟到的 _on_first_frame 幂等忽略。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    ctrl.handle_hotkey_press("toggle_enabled")
    assert ctrl.state == AppState.DISABLED

    # 迟到的首帧回调：状态非 PREPARING，幂等
    ctrl._on_first_frame()
    assert ctrl.state == AppState.DISABLED


def test_default_wait_first_frame_false_press_goes_straight_to_recording() -> None:
    """默认 wait_first_frame=False：press 后立即 RECORDING（保护旧行为）。"""
    ctrl, cb, _, recorder, _ = _make_controller()  # 默认 False

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.RECORDING
    assert ctrl._first_frame_timer is None

    # _on_first_frame 即便被调也是幂等（不是 PREPARING）
    ctrl._on_first_frame()
    assert ctrl.state == AppState.RECORDING

    # 收尾
    cb.text_event.clear()
    ctrl.handle_hotkey_release("dictate_zh")
    assert _wait(cb.text_event)
    _wait_for_state(cb, AppState.IDLE)


def test_wait_first_frame_shutdown_cancels_timer() -> None:
    """PREPARING 等待首帧时 shutdown：取消 timer + discard + 回 IDLE。"""
    ctrl, cb, _, recorder, _ = _make_controller(wait_first_frame=True)

    ctrl.handle_hotkey_press("dictate_zh")
    assert ctrl.state == AppState.PREPARING
    assert ctrl._first_frame_timer is not None

    ctrl.shutdown()
    assert ctrl._first_frame_timer is None
    assert recorder.discard_calls == 1
    assert ctrl.state == AppState.IDLE
