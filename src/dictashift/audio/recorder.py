"""音频录制服务 (sounddevice)。

AudioRecorder 用 sounddevice.InputStream 录制 float32 单声道音频，
默认 16000 Hz，供后续 ASR 使用。

不变量 (AGENTS.md 5.1 / 11)：
- 音频回调 (_callback) 内只做一件事：把 indata 拷贝 append 到内部 list；
  严禁磁盘 I/O、网络、模型推理、不抛未处理异常 (回调内 try/except 包住，
  异常仅记 logging)。
- 达到 max_seconds 自动停止：回调内只置标记并调用 on_auto_stop 通知上层，
  实际 stop()/stream.close() 由上层在 on_auto_stop 驱动下调用 (更简单可靠，
  避免 PortAudio 回调内关闭流带来的重入风险)。
- stop() 返回拼接后的 np.ndarray (float32, shape=(n,))；一帧都没录到返回空数组。
- start/stop/discard 需线程安全 (threading.Lock)；重复 start 抛 RuntimeError。
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioRecorder:
    """sounddevice 录音器。

    典型用法：
        rec = AudioRecorder(sample_rate=16000, max_seconds=60,
                            on_auto_stop=lambda: controller.on_auto_stop())
        rec.start()                 # 按下快捷键
        samples = rec.stop()        # 松开快捷键 -> 拿到 float32 数组
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        max_seconds: int = 60,
        on_auto_stop=None,
    ) -> None:
        self._sample_rate = int(sample_rate)
        self._max_seconds = int(max_seconds)
        self._on_auto_stop = on_auto_stop

        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        # 录音缓冲：保存每帧的 float32 副本，stop 时拼接。
        # 注意：必须 copy()，因为 sounddevice 回调中 indata 是复用的缓冲区。
        self._frames: list[np.ndarray] = []
        self._samples_recorded = 0  # 累计采样数，用于自动停止判定
        self._auto_stopped = False   # 标记是否已触发自动停止，避免重复通知

    # ==================== 只读属性 ====================

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._stream is not None

    # ==================== 生命周期 ====================

    def start(self) -> None:
        """打开 InputStream 开始录音。重复 start 抛 RuntimeError。"""
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("AudioRecorder 正在录音，不能重复 start")
            self._frames = []
            self._samples_recorded = 0
            self._auto_stopped = False
            # InputStream 在 __init__ 时即打开流；这里用 try 包住以在失败时
            # 不留下半初始化状态。
            stream = sd.InputStream(
                samplerate=self._sample_rate,
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

    def stop(self) -> np.ndarray:
        """停止录音并返回拼接后的 float32 单声道数组。

        一帧都没录到时返回 shape=(0,) 的 float32 空数组。
        未在录音时调用返回空数组 (安全调用)。
        """
        with self._lock:
            stream = self._stream
            self._stream = None
            frames = self._frames
            self._frames = []
            self._samples_recorded = 0
            self._auto_stopped = False

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - 关闭异常不影响已录制数据
                logger.exception("[AudioRecorder] stop/close 流时异常")

        if not frames:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).reshape(-1)

    def discard(self) -> None:
        """停止并丢弃本次录音 (打断/禁用场景)。未在录音时安全调用。"""
        with self._lock:
            stream = self._stream
            self._stream = None
            self._frames = []
            self._samples_recorded = 0
            self._auto_stopped = False

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                logger.exception("[AudioRecorder] discard 关闭流时异常")

    # ==================== 音频回调 (PortAudio 线程内执行) ====================

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        """sounddevice 输入回调。在 PortAudio 高优先级线程内执行。

        铁律 (AGENTS.md 11)：只做拷贝 append + 自动停止判定，不做任何阻塞工作，
        不抛未处理异常。
        """
        try:
            # 必须 copy()：indata 是 sounddevice 复用的内部缓冲，回调返回后会被覆盖。
            # 用局部引用抓取当前缓冲列表：若 stop()/discard() 恰好在此刻换走了
            # self._frames，本帧会落入已被丢弃的旧列表（即"停止后的尾帧被丢弃"），
            # 而不会污染新一次录音的缓冲。
            buf = self._frames
            buf.append(indata.copy())
            self._samples_recorded += int(frames)

            # 自动停止判定：累计采样数达到上限即通知上层。
            # 这里不直接调用 self.stop()——sounddevice 允许在回调中 abort/close，
            # 但由上层在 on_auto_stop 中驱动 stop() 更简单可靠，且避免重入锁
            # (stop() 会获取 self._lock，而此处并未持锁，逻辑上可行但增加复杂度)。
            if (
                self._on_auto_stop is not None
                and not self._auto_stopped
                and self._samples_recorded >= self._sample_rate * self._max_seconds
            ):
                self._auto_stopped = True
                try:
                    self._on_auto_stop()
                except Exception:  # noqa: BLE001 - 上层回调异常不得打死音频线程
                    logger.exception("[AudioRecorder] on_auto_stop 回调抛出异常")
        except Exception:  # noqa: BLE001 - 回调内严禁抛未处理异常
            logger.exception("[AudioRecorder] 音频回调处理异常")
