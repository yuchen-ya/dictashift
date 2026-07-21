"""AudioRecorder 与 list_input_devices 测试（不依赖真实麦克风）。

测试目标 (AGENTS.md 5.1 / 13.1)：
- AudioRecorder：未 start 时 stop 安全；重复 start 抛 RuntimeError。
- monkeypatch sounddevice.InputStream 为 fake（记录参数、start/stop/close 空操作、可手动喂回调数据）。
- callback 只 append；喂 indata 后 stop 返回拼接的 float32 一维数组；空录音返回空数组。
- list_input_devices：monkeypatch sd.query_devices 返回混合输入/输出设备 -> 只返回输入设备。
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from dictashift.audio import devices as devices_mod
from dictashift.audio import recorder as recorder_mod


# ==================== Fake InputStream ====================


class FakeInputStream:
    """记录构造参数、可手动喂回调数据的 fake sounddevice.InputStream。"""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.callback = kwargs.get("callback")
        self._started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self._started = True
        self.start_calls += 1

    def stop(self) -> None:
        self._started = False
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def feed(self, samples: np.ndarray, frames: int) -> None:
        """模拟 PortAudio 回调：用 captured=True, time=0, status=0 调用 callback。"""
        self.callback(samples, frames, 0, 0)


@pytest.fixture
def fake_sd(monkeypatch):
    """monkeypatch recorder.sd.InputStream 为 FakeInputStream。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeInputStream)
    return FakeInputStream


# ==================== AudioRecorder ====================


def test_stop_without_start_returns_empty(fake_sd) -> None:
    rec = recorder_mod.AudioRecorder()
    out = rec.stop()
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert out.shape == (0,)


def test_duplicate_start_raises(fake_sd) -> None:
    rec = recorder_mod.AudioRecorder()
    rec.start()
    try:
        with pytest.raises(RuntimeError):
            rec.start()
    finally:
        rec.stop()


def test_callback_appends_and_stop_concatenates(fake_sd) -> None:
    """喂回调数据后 stop 返回拼接的 float32 一维数组。"""
    rec = recorder_mod.AudioRecorder(sample_rate=16000)
    rec.start()
    assert rec.is_recording

    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 喂两帧
    frame0 = np.full((4, 1), 0.1, dtype=np.float32)
    frame1 = np.full((6, 1), 0.2, dtype=np.float32)
    stream.feed(frame0, frames=4)
    stream.feed(frame1, frames=6)

    out = rec.stop()
    assert out.dtype == np.float32
    assert out.ndim == 1
    assert out.shape == (10,)
    np.testing.assert_allclose(out[:4], 0.1)
    np.testing.assert_allclose(out[4:], 0.2)
    assert not rec.is_recording


def test_empty_recording_after_start_no_feed_returns_empty(fake_sd) -> None:
    """start 后不喂任何数据 -> stop 返回空数组。"""
    rec = recorder_mod.AudioRecorder()
    rec.start()
    out = rec.stop()
    assert out.shape == (0,)
    assert out.dtype == np.float32


def test_discard_clears_buffer(fake_sd) -> None:
    """discard 后 stop 返回空（缓冲被清空）。"""
    rec = recorder_mod.AudioRecorder()
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream.feed(np.full((4, 1), 0.5, dtype=np.float32), frames=4)
    rec.discard()
    assert not rec.is_recording
    out = rec.stop()
    assert out.shape == (0,)


def test_input_stream_records_kwargs(fake_sd) -> None:
    """构造 InputStream 时参数正确传递。"""
    rec = recorder_mod.AudioRecorder(sample_rate=22050)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    assert stream.kwargs["samplerate"] == 22050
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "float32"
    rec.stop()


def test_auto_stop_callback_invoked(fake_sd) -> None:
    """达到 max_seconds 时 on_auto_stop 被调用（回调置标记，stop 由测试驱动）。"""
    called = threading.Event()
    rec = recorder_mod.AudioRecorder(sample_rate=16000, max_seconds=1, on_auto_stop=called.set)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 喂满 1 秒（16000 采样）
    stream.feed(np.zeros((16000, 1), dtype=np.float32), frames=16000)
    assert called.wait(timeout=2.0)
    rec.stop()


# ==================== list_input_devices ====================


def test_list_input_devices_filters_input_only(monkeypatch) -> None:
    """只返回 max_input_channels > 0 的设备。"""
    fake_devices = [
        {"name": "Mic A", "max_input_channels": 2, "default_samplerate": 48000.0},
        {"name": "Speaker B", "max_input_channels": 0, "default_samplerate": 48000.0},
        {"name": "Mic C", "max_input_channels": 1, "default_samplerate": 44100.0},
        {"name": "Output D", "max_input_channels": 0, "default_samplerate": 48000.0},
    ]
    monkeypatch.setattr(devices_mod.sd, "query_devices", lambda: fake_devices)

    result = devices_mod.list_input_devices()
    names = [d["name"] for d in result]
    assert names == ["Mic A", "Mic C"]
    # 每个元素结构正确
    for d in result:
        assert set(d.keys()) == {"name", "index", "default_samplerate"}
    assert result[0]["index"] == 0
    assert result[1]["index"] == 2


def test_list_input_devices_query_failure_returns_empty(monkeypatch) -> None:
    """query_devices 抛异常时返回空列表（不崩）。"""
    def boom():
        raise RuntimeError("sd unavailable")
    monkeypatch.setattr(devices_mod.sd, "query_devices", boom)
    assert devices_mod.list_input_devices() == []
