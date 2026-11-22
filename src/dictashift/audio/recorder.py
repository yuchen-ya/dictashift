"""音频录制服务 (sounddevice)。

AudioRecorder 用 sounddevice.InputStream 录制 float32 单声道音频，最终
返回 16000 Hz 供后续 ASR 使用。

设备采样率兼容：
- start 时用 sounddevice.query_devices(device_index)（None 取默认设备）读取
  default_samplerate；流以设备**原生**采样率打开，stop() 返回前若原生采样率
  != 16000 则用 audio.resample.resample_audio 重采样到 16000。
- query 失败时回退 16000 并记 warning。
- 可选预热在待机时维持输入流；待机帧直接丢弃，开始录音时按流时间裁去边界前的采样。

录音中途断流：
- _callback 检查 status (sounddevice CallbackFlags)；_is_fatal_status 判定
  严重错误时，置标记并通过 on_stream_error(str(status)) 通知一次（回调里只
  通知不阻塞，与 on_auto_stop 同模式）。
- Input overflow 视为偶发丢帧，仍保留当前回调收到的数据。

不变量 (AGENTS.md 5.1 / 11)：
- 音频回调 (_callback) 内只做：首帧一次性通知、拷贝 append、自动停止判定、
  致命 status 一次性通知；严禁磁盘 I/O、网络、模型推理，不抛未处理异常
  (回调内 try/except 包住，异常仅记 logging)。
- 达到 max_seconds 自动停止：回调内只置标记并调用 on_auto_stop 通知上层，
  实际 stop()/stream.close() 由上层在 on_auto_stop 驱动下调用 (更简单可靠，
  避免 PortAudio 回调内关闭流带来的重入风险)。
- stop() 返回 16000 Hz float32 单声道 np.ndarray (shape=(n,))；
  一帧都没录到返回空数组。
- start/stop/discard 需线程安全 (threading.Lock)。
- 重复 start 自愈：若上次流因上层状态失步未被释放，start 强制关闭残留流
  后继续（不抛错），从「麦克风永久锁死」状态恢复。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

from .resample import resample_audio

logger = logging.getLogger(__name__)

# ASR 固定目标采样率。
_TARGET_SR = 16000


def _is_fatal_status(status) -> bool:
    """判定 sounddevice 回调 status (CallbackFlags) 是否为致命故障。

    设计（AGENTS.md 11 + 任务目标）：
    - Input overflow：偶发丢帧，不算故障（仅 debug 日志）。同理 input_underflow
      也是输入流可能发生的良性抖动。
    - priming_output：流刚启动时的准备阶段提示，sounddevice 文档明确为非错误。
    - 其他任何 status（output_underflow / output_overflow 对纯输入流本不应出现，
      或未来扩展的未知 flag）：视为严重错误（如设备被拔/异常）。
    - status 为空（bool(status) is False）一定不是致命。

    纯函数，可独立单测。
    """
    if not status:
        return False
    # 取出底层 flag 位（CallbackFlags.__init__(flags=0x0)，__slots__ = '_flags'）。
    # 兼容 fake status（直接 int 或无 _flags）。
    try:
        bits = int(getattr(status, "_flags", status))
    except (TypeError, ValueError):
        # 无法解析为整数时保守视为非致命（避免误报打死正常录音）。
        return False

    # sounddevice/PortAudio 的 paCallbackReturn 位定义：
    #   paContinue              = 0
    #   paComplete              = 1   (流自然结束，非错误)
    #   paAbort                 = 2   (流被中止 -> 致命)
    # 以及 CallbackFlags 的位：input_underflow=1, input_overflow=2,
    # output_underflow=4, output_overflow=8, priming_output=16。
    # 见 PortAudio paCallbackFlags / sounddevice CallbackFlags。
    _INPUT_UNDERFLOW = 1 << 0
    _INPUT_OVERFLOW = 1 << 1
    _OUTPUT_UNDERFLOW = 1 << 2
    _OUTPUT_OVERFLOW = 1 << 3
    _PRIMING_OUTPUT = 1 << 4
    _PA_ABORT = 1 << 1  # 注意：与 input_overflow 位冲突，但 abort 来自不同的返回路径；
    # sounddevice 回调里 status 是 CallbackFlags（不含 paAbort），这里仅按 flag 位处理。

    benign = _INPUT_UNDERFLOW | _INPUT_OVERFLOW | _PRIMING_OUTPUT
    # 任何非良性位被置 -> 致命（output_* 对纯输入流不应出现）。
    if bits & ~benign:
        return True
    return False


class AudioRecorder:
    """sounddevice 录音器。

    典型用法：
        rec = AudioRecorder(sample_rate=16000, max_seconds=60,
                            on_auto_stop=lambda: controller.on_auto_stop(),
                            on_stream_error=lambda msg: ...)
        rec.start()                 # 按下快捷键（若上次流残留会自愈强制关闭）
        samples = rec.stop()        # 松开快捷键 -> 拿到 float32 (16kHz) 数组

    参数：
        sample_rate: ASR 目标采样率（固定 16000，保持参数仅为兼容）。
        max_seconds: 单次录音上限，达到则 on_auto_stop 触发。
        on_auto_stop: 达到上限时回调（PortAudio 线程，只通知不阻塞）。
        device_index: 输入设备索引；None 用系统默认输入设备。
        on_stream_error: 录音中途严重 status（_is_fatal_status）时回调，
            最多触发一次（PortAudio 线程，只通知不阻塞）。
        on_first_frame: 每次 start() 后首个音频回调帧到达时触发一次
            （PortAudio 线程，只通知不阻塞）。用于上层把 PREPARING -> RECORDING
            的迁移推迟到「真的在收音」之时。stop / discard 后挂起的回调不再触发。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        max_seconds: int = 60,
        on_auto_stop: Callable[[], None] | None = None,
        device_index: int | None = None,
        on_stream_error: Callable[[str], None] | None = None,
        on_first_frame: Callable[[], None] | None = None,
    ) -> None:
        # ASR 固定 16k；保留 sample_rate 参数以兼容旧调用，但实际目标恒为 _TARGET_SR。
        self._sample_rate = int(sample_rate) if sample_rate else _TARGET_SR
        self._max_seconds = int(max_seconds)
        self._on_auto_stop = on_auto_stop
        self._device_index = device_index
        self._on_stream_error = on_stream_error
        # 每次 start() 后首个音频回调到达时触发一次（PortAudio 线程，只通知不阻塞）。
        # 用于上层把 PREPARING -> RECORDING 的迁移推迟到「真的在收音」之时
        # （蓝牙设备 stream.start() 立即返回，但首个回调要数百毫秒后才到）。
        self._on_first_frame = on_first_frame
        # 标记本轮是否仍在等待首个回调帧。start() 置 True；首帧到达 / stop / discard
        # 后置 False。仅在 _on_first_frame 非 None 时才有意义，但无条件维护以简化逻辑。
        self._first_frame_pending: bool = False

        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stream_marker: object | None = None
        self._stream: sd.InputStream | None = None
        self._warm_enabled = False
        self._recording = False
        self._recording_started_at: float | None = None
        # 录音缓冲：保存每帧的 float32 副本，stop 时拼接。
        # 注意：必须 copy()，因为 sounddevice 回调中 indata 是复用的缓冲区。
        self._frames: list[np.ndarray] = []
        self._samples_recorded = 0  # 累计采样数（按设备原生采样率计），用于自动停止判定
        self._auto_stopped = False   # 标记是否已触发自动停止，避免重复通知
        # 本轮流打开时使用的设备**原生**采样率。stop 时据此决定是否重采样。
        self._native_sr: int = _TARGET_SR
        # 标记本轮是否已通知过 on_stream_error（致命 status 最多通知一次）。
        self._stream_error_notified = False

    # ==================== 只读属性 ====================

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def device_index(self) -> int | None:
        return self._device_index

    @device_index.setter
    def device_index(self, value: int | None) -> None:
        """运行时更换输入设备；仅允许在未录音时调用（录音中更换抛 RuntimeError）。"""
        with self._lifecycle_lock:
            if self._recording:
                raise RuntimeError("录音中不能更换输入设备")
            if value == self._device_index:
                return
            self._close_stream(self._take_recording()[0])
            self._device_index = value
            if self._warm_enabled:
                self._warm_input()

    @property
    def max_seconds(self) -> int:
        return self._max_seconds

    @max_seconds.setter
    def max_seconds(self, value: int) -> None:
        """运行时调整最长录音秒数；仅允许在未录音时调用。"""
        with self._lifecycle_lock:
            if self._recording:
                raise RuntimeError("录音中不能调整最长录音时长")
            self._max_seconds = int(value)

    # ==================== 生命周期 ====================

    def _query_native_samplerate(self, device: int | None) -> int:
        """查询设备原生采样率，失败回退 16k 并记 warning。

        注意：device_index=None 时不能调用 sd.query_devices(None)——sounddevice
        在该参数下返回 DeviceList（无 .get 属性），会触发 AttributeError 并走入
        异常回退。正确做法是用 sd.query_devices(kind="input") 拿默认输入设备 dict。
        """
        try:
            if device is None:
                info = sd.query_devices(kind="input")
            else:
                info = sd.query_devices(device)
            sr = int(info.get("default_samplerate", _TARGET_SR))
            if sr <= 0:
                return _TARGET_SR
            return sr
        except Exception:  # noqa: BLE001 - 查询失败回退默认，不阻塞录音
            logger.warning(
                "[AudioRecorder] query_devices(device_index=%r) 失败，回退 %d Hz",
                device, _TARGET_SR, exc_info=True,
            )
            return _TARGET_SR

    def set_warm_enabled(self, enabled: bool) -> bool:
        """开启预热并报告是否可用。关闭时释放空闲流，当前录音结束后释放其流。"""
        with self._lifecycle_lock:
            self._warm_enabled = enabled
            if not enabled:
                if not self._recording:
                    self._close_stream(self._take_recording()[0])
                return False
            return self._recording or self._warm_input()

    def close(self) -> None:
        """关闭预热、丢弃本次录音并释放麦克风。"""
        with self._lifecycle_lock:
            self._warm_enabled = False
            self._close_stream(self._take_recording()[0])

    def start(self) -> None:
        """开始收集音频，优先复用可用的预热流。"""
        with self._lifecycle_lock:
            if self._stream is not None and not self._recording and self._warm_enabled:
                try:
                    started_at = self._stream.time if self._stream.active else None
                except sd.PortAudioError:
                    started_at = None
                if started_at is not None and math.isfinite(started_at):
                    with self._lock:
                        if not self._stream_error_notified:
                            self._begin_recording_locked(started_at)
                            return
            if self._stream is not None:
                logger.warning("[AudioRecorder] 关闭上次残留的录音流")
                self._close_stream(self._take_recording()[0])
            self._open_input(recording=True)

    def _warm_input(self) -> bool:
        if self._stream is not None:
            try:
                active = self._stream.active
            except sd.PortAudioError:
                active = False
            with self._lock:
                if active and not self._stream_error_notified:
                    return True
            self._close_stream(self._take_recording()[0])

        try:
            self._open_input(recording=False)
        except sd.PortAudioError as exc:
            logger.warning("[AudioRecorder] 麦克风预热失败，听写仍可按需启动: %s", exc)
            return False
        # 有些设备会在 start() 返回前先报告错误。
        with self._lock:
            failed = self._stream_error_notified
        if failed:
            self._close_stream(self._take_recording()[0])
        return not failed

    def _open_input(self, *, recording: bool) -> None:
        device = self._device_index
        if device is None:
            try:
                wasapi = next(
                    (api["default_input_device"] for api in sd.query_hostapis()
                     if api["name"] == "Windows WASAPI"),
                    -1,
                )
                if wasapi >= 0:
                    self._open_stream(wasapi, recording=recording)
                    return
            except sd.PortAudioError as exc:
                logger.info("[AudioRecorder] WASAPI 输入不可用，使用兼容设备: %s", exc)
        self._open_stream(device, recording=recording)

    def _open_stream(self, device: int | None, *, recording: bool) -> None:
        started = time.perf_counter()
        native_sr = self._query_native_samplerate(device)
        marker = object()

        def callback(indata, frames, timing, status):
            self._callback(indata, frames, timing, status, marker)

        stream = sd.InputStream(
            samplerate=native_sr,
            device=device,
            channels=1,
            dtype="float32",
            latency="low",
            callback=callback,
        )
        with self._lock:
            self._stream = stream
            self._stream_marker = marker
            self._native_sr = native_sr
            if recording:
                self._begin_recording_locked()
        try:
            # 某些驱动在 start 返回前已回调，不能持有缓冲区锁。
            stream.start()
        except Exception:
            self._take_recording()
            try:
                stream.close()
            except Exception:
                logger.exception("[AudioRecorder] 启动失败后关闭流异常")
            raise
        logger.debug("[AudioRecorder] 打开并启动录音流耗时 %.1f ms", (time.perf_counter() - started) * 1000)

    def _begin_recording_locked(self, started_at: float | None = None) -> None:
        self._recording_started_at = started_at
        self._first_frame_pending = self._on_first_frame is not None
        self._recording = True

    def _take_recording(
        self, *, keep_warm: bool = False,
    ) -> tuple[sd.InputStream | None, list[np.ndarray], int]:
        with self._lock:
            stream = None
            frames, native_sr = self._frames, self._native_sr
            if not (keep_warm and self._warm_enabled and not self._stream_error_notified):
                stream, self._stream = self._stream, None
                self._stream_marker = None
                self._stream_error_notified = False
                self._native_sr = _TARGET_SR
            self._recording = False
            self._recording_started_at = None
            self._frames = []
            self._samples_recorded = 0
            self._auto_stopped = False
            self._first_frame_pending = False
        return stream, frames, native_sr

    @staticmethod
    def _close_stream(stream: sd.InputStream | None) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logger.exception("[AudioRecorder] stop 流时异常")
        try:
            stream.close()
        except Exception:
            logger.exception("[AudioRecorder] close 流时异常")

    def stop(self) -> np.ndarray:
        """停止录音并返回拼接后的 float32 单声道数组（**始终 16 kHz**）。

        一帧都没录到时返回 shape=(0,) 的 float32 空数组。
        未在录音时调用返回空数组 (安全调用)。
        若设备原生采样率 != 16000，重采样到 16000 后返回（ASR 接口不变）。
        """
        with self._lifecycle_lock:
            stream, frames, native_sr = self._take_recording(keep_warm=True)
            self._close_stream(stream)

        if not frames:
            return np.empty(0, dtype=np.float32)
        audio = np.concatenate(frames, axis=0).reshape(-1)
        # 重采样到 ASR 目标采样率（16k）。相同采样率时 resample_audio 原样返回。
        if native_sr != _TARGET_SR:
            try:
                audio = resample_audio(audio, native_sr, _TARGET_SR)
                # 统一 dtype 为 float32（resample_audio 返回 float32，保险起见 cast）。
                audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            except Exception:  # noqa: BLE001 - 重采样失败不能返回错乱数据
                logger.exception(
                    "[AudioRecorder] %d -> %d 重采样失败", native_sr, _TARGET_SR
                )
                return np.empty(0, dtype=np.float32)
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    def discard(self) -> None:
        """停止并丢弃本次录音。"""
        with self._lifecycle_lock:
            self._close_stream(self._take_recording(keep_warm=True)[0])

    def _callback(self, indata: np.ndarray, frames: int, timing, status, marker=None) -> None:
        """仅保留当前录音的采样。事件订阅只负责投递，不得回调录音器。"""
        try:
            fatal = _is_fatal_status(status)
            with self._lock:
                if self._stream is None or (marker is not None and marker is not self._stream_marker):
                    return
                if fatal:
                    first_error = not self._stream_error_notified
                    self._stream_error_notified = True
                    if not self._recording:
                        raise sd.CallbackAbort
                    if first_error and self._on_stream_error is not None:
                        self._on_stream_error(str(status))
                    return
                if not self._recording or self._stream_error_notified or frames <= 0:
                    return

                if self._recording_started_at is not None:
                    adc_time = timing.inputBufferAdcTime
                    # PortAudio 为输入块的第一帧提供采样时间，并与 stream.time 共用时钟。
                    skipped = max(0, math.ceil((self._recording_started_at - adc_time) * self._native_sr))
                    if skipped >= frames:
                        return
                    if skipped:
                        indata = indata[skipped:]
                        frames -= skipped
                    self._recording_started_at = None

                self._frames.append(indata.copy())
                self._samples_recorded += int(frames)
                # 与录音片段一起提交事件，避免待机流复用时事件越过松手/再次按下。
                if self._first_frame_pending and self._on_first_frame is not None:
                    self._first_frame_pending = False
                    try:
                        self._on_first_frame()
                    except Exception:
                        logger.exception("[AudioRecorder] on_first_frame 回调抛出异常")
                if (
                    self._on_auto_stop is not None and not self._auto_stopped
                    and self._samples_recorded >= self._native_sr * self._max_seconds
                ):
                    self._auto_stopped = True
                    self._on_auto_stop()
        except sd.CallbackAbort:
            raise
        except Exception:
            logger.exception("[AudioRecorder] 音频回调处理异常")
