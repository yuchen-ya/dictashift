"""音频录制服务 (sounddevice)。

AudioRecorder 用 sounddevice.InputStream 录制 float32 单声道音频，最终
返回 16000 Hz 供后续 ASR 使用。

设备采样率兼容：
- start 时用 sounddevice.query_devices(device_index)（None 取默认设备）读取
  default_samplerate；流以设备**原生**采样率打开，stop() 返回前若原生采样率
  != 16000 则用 audio.resample.resample_audio 重采样到 16000。
- query 失败时回退 16000 并记 warning。

录音中途断流：
- _callback 检查 status (sounddevice CallbackFlags)；_is_fatal_status 判定
  严重错误时，置标记并通过 on_stream_error(str(status)) 通知一次（回调里只
  通知不阻塞，与 on_auto_stop 同模式）。
- Input overflow 仅记 debug 日志（偶发丢帧不算故障）。

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
import threading
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
        self._stream: sd.InputStream | None = None
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
            return self._stream is not None

    @property
    def device_index(self) -> int | None:
        return self._device_index

    @device_index.setter
    def device_index(self, value: int | None) -> None:
        """运行时更换输入设备；仅允许在未录音时调用（录音中更换抛 RuntimeError）。"""
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("录音中不能更换输入设备")
            self._device_index = value

    @property
    def max_seconds(self) -> int:
        return self._max_seconds

    @max_seconds.setter
    def max_seconds(self, value: int) -> None:
        """运行时调整最长录音秒数；仅允许在未录音时调用。"""
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("录音中不能调整最长录音时长")
            self._max_seconds = int(value)

    # ==================== 生命周期 ====================

    def _query_native_samplerate(self) -> int:
        """查询设备原生采样率，失败回退 16k 并记 warning。

        注意：device_index=None 时不能调用 sd.query_devices(None)——sounddevice
        在该参数下返回 DeviceList（无 .get 属性），会触发 AttributeError 并走入
        异常回退。正确做法是用 sd.query_devices(kind="input") 拿默认输入设备 dict。
        """
        try:
            if self._device_index is None:
                info = sd.query_devices(kind="input")
            else:
                info = sd.query_devices(self._device_index)
            sr = int(info.get("default_samplerate", _TARGET_SR))
            if sr <= 0:
                return _TARGET_SR
            return sr
        except Exception:  # noqa: BLE001 - 查询失败回退默认，不阻塞录音
            logger.warning(
                "[AudioRecorder] query_devices(device_index=%r) 失败，回退 %d Hz",
                self._device_index, _TARGET_SR, exc_info=True,
            )
            return _TARGET_SR

    def start(self) -> None:
        """打开 InputStream 开始录音。

        自愈语义：若发现上一次的流未被正常释放（self._stream 残留，说明上层
        状态机失步，例如旧 worker 收尾把新任务状态踩成 IDLE 导致
        handle_hotkey_release 静默 return 未关流），不抛错——先取走残留引用并
        置 None，在锁外强制关闭旧流后继续正常 start 流程。这样可从「麦克风
        永久锁死」状态自愈，避免每次 start 都抛 RuntimeError。
        """
        # 残留旧流引用（若上层失步导致上次流未关闭）。锁内只取引用并置 None，
        # 真正的 stop/close 放到锁外执行（与 stop() 一致：关流可能阻塞）。
        stale_stream: sd.InputStream | None = None
        try:
            with self._lock:
                if self._stream is not None:
                    logger.warning(
                        "[AudioRecorder] start 时发现上次流未释放（上层状态失步），强制关闭后自愈"
                    )
                    stale_stream = self._stream
                    self._stream = None
                self._frames = []
                self._samples_recorded = 0
                self._auto_stopped = False
                self._stream_error_notified = False
                # 标记本轮等待首个回调帧（_callback 中据此触发一次性 on_first_frame）。
                self._first_frame_pending = self._on_first_frame is not None
                # 查询设备原生采样率：流以原生采样率打开，stop 时重采样到 16k。
                native_sr = self._query_native_samplerate()
                self._native_sr = native_sr
                # InputStream 在 __init__ 时即打开流；这里用 try 包住以在失败时
                # 不留下半初始化状态。samplerate 用设备原生采样率，避免 sounddevice
                # 内部做隐式重采样（部分驱动会因此报错或返回异常采样）。
                stream = sd.InputStream(
                    samplerate=native_sr,
                    device=self._device_index,
                    channels=1,
                    dtype="float32",
                    callback=self._callback,
                )
                try:
                    stream.start()
                except Exception:
                    # 启动失败时关闭并清理，向上抛出原始异常。
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        logger.exception("[AudioRecorder] 启动失败后关闭流再次异常")
                    raise
                self._stream = stream
        finally:
            # finally 保证：即使新流构造/start 抛异常，旧流也一定被关闭
            # （异常向上传播前先收尾旧流，避免句柄泄漏）。拆分 try：stop 失败
            # 仍要尝试 close。
            if stale_stream is not None:
                try:
                    stale_stream.stop()
                except Exception:  # noqa: BLE001 - 关闭异常不影响新流
                    logger.exception("[AudioRecorder] 自愈关闭残留旧流 stop 异常")
                try:
                    stale_stream.close()
                except Exception:  # noqa: BLE001
                    logger.exception("[AudioRecorder] 自愈关闭残留旧流 close 异常")

    def stop(self) -> np.ndarray:
        """停止录音并返回拼接后的 float32 单声道数组（**始终 16 kHz**）。

        一帧都没录到时返回 shape=(0,) 的 float32 空数组。
        未在录音时调用返回空数组 (安全调用)。
        若设备原生采样率 != 16000，重采样到 16000 后返回（ASR 接口不变）。
        """
        with self._lock:
            stream = self._stream
            native_sr = self._native_sr
            self._stream = None
            frames = self._frames
            self._frames = []
            self._samples_recorded = 0
            self._auto_stopped = False
            self._stream_error_notified = False
            # pending 已无意义：本轮要么首帧已到（pending 早被清），要么 stop 后
            # 不再期待任何回调；置 False 防止 stop 后到达的尾帧误触发 on_first_frame。
            self._first_frame_pending = False
            self._native_sr = _TARGET_SR

        if stream is not None:
            # 拆分 try：stop 失败仍要尝试 close（否则句柄泄漏）。
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 - 关闭异常不影响已录制数据
                logger.exception("[AudioRecorder] stop 流时异常")
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                logger.exception("[AudioRecorder] close 流时异常")

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
        """停止并丢弃本次录音 (打断/禁用场景)。未在录音时安全调用。"""
        with self._lock:
            stream = self._stream
            self._stream = None
            self._frames = []
            self._samples_recorded = 0
            self._auto_stopped = False
            self._stream_error_notified = False
            # 同 stop：discard 后挂起的回调不再触发 on_first_frame。
            self._first_frame_pending = False
            self._native_sr = _TARGET_SR

        if stream is not None:
            # 拆分 try：stop 失败仍要尝试 close（否则句柄泄漏）。
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                logger.exception("[AudioRecorder] discard stop 流时异常")
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                logger.exception("[AudioRecorder] discard close 流时异常")

    # ==================== 音频回调 (PortAudio 线程内执行) ====================

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        """sounddevice 输入回调。在 PortAudio 高优先级线程内执行。

        铁律 (AGENTS.md 11)：只做拷贝 append + 自动停止判定 + 致命 status
        一次性通知，不做任何阻塞工作，不抛未处理异常。
        """
        try:
            # 0) 首帧通知：本轮 start 后首个回调到达时触发一次 on_first_frame
            #    （只通知不阻塞，异常只记日志）。这是「真的在收音」的准确信号——
            #    蓝牙设备 stream.start() 立即返回，但首个回调要数百毫秒后才到。
            if self._first_frame_pending:
                # 先清标记（无锁：仅本回调线程读写，且 stop/discard 也会清；
                # 即便竞态导致多触发一次 on_first_frame，上层 _on_first_frame 是幂等的）。
                self._first_frame_pending = False
                if self._on_first_frame is not None:
                    try:
                        self._on_first_frame()
                    except Exception:  # noqa: BLE001 - 上层回调异常不得打死音频线程
                        logger.exception(
                            "[AudioRecorder] on_first_frame 回调抛出异常"
                        )

            # 1) status 判定（Input overflow 仅 debug 日志；致命错误一次性通知）。
            if status:
                # input_overflow 是输入流的偶发丢帧，正常现象，仅 debug。
                # 其余 flag 由 _is_fatal_status 判定。
                try:
                    input_overflow = bool(getattr(status, "input_overflow", False))
                except Exception:  # noqa: BLE001
                    input_overflow = False
                if input_overflow:
                    logger.debug("[AudioRecorder] input overflow（偶发丢帧，忽略）")
                if _is_fatal_status(status):
                    # 致命错误（设备被拔/严重异常）：最多通知一次。
                    already = False
                    with self._lock:
                        already = self._stream_error_notified
                        if not already:
                            self._stream_error_notified = True
                    if not already and self._on_stream_error is not None:
                        try:
                            self._on_stream_error(str(status))
                        except Exception:  # noqa: BLE001 - 上层回调异常不得打死音频线程
                            logger.exception(
                                "[AudioRecorder] on_stream_error 回调抛出异常"
                            )

            # 2) 拷贝 append（必须 copy()：indata 是复用的内部缓冲）。
            # 用局部引用抓取当前缓冲列表：若 stop()/discard() 恰好在此刻换走了
            # self._frames，本帧会落入已被丢弃的旧列表（即"停止后的尾帧被丢弃"），
            # 而不会污染新一次录音的缓冲。
            buf = self._frames
            buf.append(indata.copy())
            self._samples_recorded += int(frames)

            # 3) 自动停止判定：累计采样数达到上限即通知上层。
            # 这里不直接调用 self.stop()——sounddevice 允许在回调中 abort/close，
            # 但由上层在 on_auto_stop 中驱动 stop() 更简单可靠，且避免重入锁
            # (stop() 会获取 self._lock，而此处并未持锁，逻辑上可行但增加复杂度)。
            # 自动停止以**原生**采样率累计：max_seconds * native_sr 个采样。
            if (
                self._on_auto_stop is not None
                and not self._auto_stopped
                and self._samples_recorded >= self._native_sr * self._max_seconds
            ):
                self._auto_stopped = True
                try:
                    self._on_auto_stop()
                except Exception:  # noqa: BLE001 - 上层回调异常不得打死音频线程
                    logger.exception("[AudioRecorder] on_auto_stop 回调抛出异常")
        except Exception:  # noqa: BLE001 - 回调内严禁抛未处理异常
            logger.exception("[AudioRecorder] 音频回调处理异常")
