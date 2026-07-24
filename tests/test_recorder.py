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
    """monkeypatch recorder.sd.InputStream 为 FakeInputStream，并默认把
    query_devices 伪造成返回 16kHz 原生采样率的设备信息（多数测试沿用 16k）。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeInputStream)
    _default = {"name": "fake-mic", "default_samplerate": 16000.0, "max_input_channels": 1}

    def _query_devices(device=None, *args, **kwargs):
        return _default

    monkeypatch.setattr(recorder_mod.sd, "query_devices", _query_devices)
    return FakeInputStream


@pytest.fixture
def fake_query(monkeypatch):
    """自包含 fixture：同时 patch InputStream 与 query_devices，返回可改写的
    状态容器。测试中设置 fake_query['sr'] = 48000 即可改变设备原生采样率。

    用法：fake_query['sr'] = 48000（默认 16k）
    不与 fake_sd 同时使用（避免 query_devices 互相覆盖）。
    """
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeInputStream)
    state = {"sr": 16000.0}

    def _query_devices(device=None, *args, **kwargs):
        return {"name": "fake-mic", "default_samplerate": state["sr"], "max_input_channels": 1}

    monkeypatch.setattr(recorder_mod.sd, "query_devices", _query_devices)
    return state


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
    """构造 InputStream 时参数正确传递。

    新行为：流采样率取自设备原生采样率（query_devices，fake_sd 默认 16k），
    不再直接用 sample_rate 构造参数。device_index 透传给 InputStream。
    """
    rec = recorder_mod.AudioRecorder(sample_rate=16000, device_index=7)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # samplerate 走设备原生（默认 fake 设备 16k）
    assert stream.kwargs["samplerate"] == 16000
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "float32"
    # device_index 透传
    assert stream.kwargs["device"] == 7
    rec.stop()


def test_device_index_defaults_to_none(fake_sd) -> None:
    """不传 device_index 时 InputStream 收到 device=None（默认输入设备）。"""
    rec = recorder_mod.AudioRecorder()
    assert rec.device_index is None
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    assert stream.kwargs["device"] is None
    rec.stop()


def test_device_index_passed_through(monkeypatch) -> None:
    """device_index 透传到 InputStream 与 query_devices。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeInputStream)
    calls: list = []

    def _q(device=None, *a, **kw):
        calls.append(device)
        return {"name": "x", "default_samplerate": 16000.0, "max_input_channels": 1}

    monkeypatch.setattr(recorder_mod.sd, "query_devices", _q)
    rec = recorder_mod.AudioRecorder(device_index=3)
    rec.start()
    assert calls and calls[-1] == 3
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    assert stream.kwargs["device"] == 3
    rec.stop()


def test_non_native_sr_resampled_to_16k(fake_query) -> None:
    """设备原生 48k -> 流以 48k 打开，stop 返回 16k 长度比例正确（1/3）。"""
    fake_query["sr"] = 48000.0
    rec = recorder_mod.AudioRecorder(device_index=0)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 流确实以 48k 打开
    assert stream.kwargs["samplerate"] == 48000
    # 喂入 4800 采样（48k 下的 0.1 秒）
    frame = np.zeros((4800, 1), dtype=np.float32)
    stream.feed(frame, frames=4800)
    out = rec.stop()
    # 48k -> 16k：长度应为 1600 ±1（重采样长度按 ceil 比例）
    assert out.dtype == np.float32
    assert out.ndim == 1
    assert abs(out.shape[0] - 1600) <= 2


def test_native_16k_passthrough_no_resample(fake_sd) -> None:
    """设备原生 16k -> 不重采样，stop 返回原始长度。"""
    rec = recorder_mod.AudioRecorder()
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream.feed(np.full((1000, 1), 0.5, dtype=np.float32), frames=1000)
    out = rec.stop()
    assert out.shape == (1000,)
    np.testing.assert_allclose(out, 0.5)


def test_query_devices_failure_falls_back_to_16k(monkeypatch, fake_sd) -> None:
    """query_devices 抛异常时回退 16k，流以 16k 打开。"""
    def boom(*a, **kw):
        raise RuntimeError("sd unavailable")
    monkeypatch.setattr(recorder_mod.sd, "query_devices", boom)
    rec = recorder_mod.AudioRecorder(device_index=2)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    assert stream.kwargs["samplerate"] == 16000
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


def test_auto_stop_uses_native_sr(fake_query) -> None:
    """自动停止按设备原生采样率累计：48k 下 max_seconds=1 需 48000 采样触发。"""
    fake_query["sr"] = 48000.0
    called = threading.Event()
    rec = recorder_mod.AudioRecorder(sample_rate=16000, max_seconds=1, on_auto_stop=called.set)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 喂 47999 采样（48k 下不足 1 秒）-> 不应触发
    stream.feed(np.zeros((47999, 1), dtype=np.float32), frames=47999)
    assert not called.wait(timeout=0.2)
    # 再喂 1 采样 -> 达到 48000，触发
    stream.feed(np.zeros((1, 1), dtype=np.float32), frames=1)
    assert called.wait(timeout=2.0)
    rec.stop()


# ==================== _is_fatal_status 纯函数 ====================


def test_is_fatal_status_empty_not_fatal() -> None:
    from dictashift.audio.recorder import _is_fatal_status
    import sounddevice as sd
    # 空 CallbackFlags / 0 / None 都不是致命
    assert _is_fatal_status(sd.CallbackFlags(0)) is False
    assert _is_fatal_status(None) is False
    assert _is_fatal_status(0) is False


def test_is_fatal_status_input_overflow_not_fatal() -> None:
    from dictashift.audio.recorder import _is_fatal_status
    import sounddevice as sd
    # input_overflow 位 = 2，input_underflow = 1，priming_output = 16：均良性
    assert _is_fatal_status(sd.CallbackFlags(2)) is False
    assert _is_fatal_status(sd.CallbackFlags(1)) is False
    assert _is_fatal_status(sd.CallbackFlags(16)) is False
    # 组合良性位仍非致命
    assert _is_fatal_status(sd.CallbackFlags(1 | 2 | 16)) is False


def test_is_fatal_status_output_flags_fatal() -> None:
    from dictashift.audio.recorder import _is_fatal_status
    import sounddevice as sd
    # output_underflow = 4, output_overflow = 8：纯输入流不应出现 -> 致命
    assert _is_fatal_status(sd.CallbackFlags(4)) is True
    assert _is_fatal_status(sd.CallbackFlags(8)) is True
    # input_overflow + output_overflow 组合仍致命
    assert _is_fatal_status(sd.CallbackFlags(2 | 8)) is True


def test_is_fatal_status_unknown_high_bit_fatal() -> None:
    from dictashift.audio.recorder import _is_fatal_status
    import sounddevice as sd
    # 未知高位 flag -> 保守判定为致命
    assert _is_fatal_status(sd.CallbackFlags(1 << 20)) is True


# ==================== on_stream_error 一次性通知 ====================


def _make_fatal_status():
    import sounddevice as sd
    return sd.CallbackFlags(8)  # output_overflow -> _is_fatal_status 判定为 True


def test_stream_error_notified_once_on_fatal_status(fake_sd) -> None:
    """致命 status 出现多次时 on_stream_error 只通知一次。"""
    msgs: list[str] = []

    rec = recorder_mod.AudioRecorder(on_stream_error=msgs.append)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # status 必须放在第 4 位（callback 签名 indata, frames, time, status）
    fatal = _make_fatal_status()
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    assert len(msgs) == 1
    # 消息内容是 str(status)
    assert "output overflow" in msgs[0] or "output" in msgs[0]
    rec.stop()


def test_stream_error_not_called_on_input_overflow(fake_sd) -> None:
    """input_overflow 仅 debug 日志，不触发 on_stream_error。"""
    import sounddevice as sd
    msgs: list[str] = []
    rec = recorder_mod.AudioRecorder(on_stream_error=msgs.append)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, sd.CallbackFlags(2))
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, sd.CallbackFlags(2))
    assert msgs == []
    rec.stop()


def test_stream_error_notified_again_after_new_start(fake_sd) -> None:
    """stop + 再次 start 后，新一轮录音致命 status 可再次通知一次。"""
    msgs: list[str] = []
    fatal = _make_fatal_status()

    rec = recorder_mod.AudioRecorder(on_stream_error=msgs.append)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    assert len(msgs) == 1
    rec.stop()

    # 新一轮
    rec.start()
    stream2: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream2.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    stream2.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, fatal)
    assert len(msgs) == 2
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
