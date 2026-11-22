"""AudioRecorder 与 list_input_devices 测试（不依赖真实麦克风）。

测试目标 (AGENTS.md 5.1 / 13.1)：
- AudioRecorder：未 start 时 stop 安全；start 发现残留流时自愈强制关闭后继续（不抛错）。
- monkeypatch sounddevice.InputStream 为 fake（记录参数、start/stop/close 空操作、可手动喂回调数据）。
- callback 只 append；喂 indata 后 stop 返回拼接的 float32 一维数组；空录音返回空数组。
- list_input_devices：monkeypatch sd.query_devices 返回混合输入/输出设备 -> 只返回输入设备。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

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
        self.time = 100.0
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self._started = True
        self.start_calls += 1

    @property
    def active(self) -> bool:
        return self._started

    def stop(self) -> None:
        self._started = False
        self.stop_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def feed(self, samples, frames: int, *, adc_time: float | None = None, status=0) -> None:
        """模拟 PortAudio 回调及其中止行为。"""
        timing = SimpleNamespace(inputBufferAdcTime=self.time if adc_time is None else adc_time)
        try:
            self.callback(samples, frames, timing, status)
        except recorder_mod.sd.CallbackAbort:
            self._started = False


# 用于自愈 finally 测试的可控 fake：可在 start/stop/close 上抛指定异常。
class FakeStream:
    """可控异常的 fake InputStream（类变量配置是否在各方法抛错）。"""

    # 类级开关：测试用例临时置 True 触发对应方法抛错。
    start_raises: bool = False
    stop_raises: bool = False
    close_raises: bool = False

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.callback = kwargs.get("callback")
        self.stop_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        if FakeStream.start_raises:
            raise RuntimeError("start boom")

    def stop(self) -> None:
        self.stop_calls += 1
        if FakeStream.stop_raises:
            raise RuntimeError("stop boom")

    def close(self) -> None:
        self.close_calls += 1
        if FakeStream.close_raises:
            raise RuntimeError("close boom")


@pytest.fixture(autouse=True)
def no_wasapi_by_default(monkeypatch):
    monkeypatch.setattr(recorder_mod.sd, "query_hostapis", lambda: ())


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


@pytest.mark.parametrize("wasapi_available", [True, False])
def test_default_input_prefers_wasapi_with_compatible_fallback(monkeypatch, fake_sd, wasapi_available):
    monkeypatch.setattr(recorder_mod.sd, "query_hostapis", lambda: (
        {"name": "Windows WASAPI", "default_input_device": 12},
    ))
    opened = []

    def open_stream(**kwargs):
        device = kwargs["device"]
        opened.append(device)
        if device == 12 and not wasapi_available:
            raise recorder_mod.sd.PortAudioError("unavailable")
        return FakeInputStream(**kwargs)

    monkeypatch.setattr(recorder_mod.sd, "InputStream", open_stream)
    rec = recorder_mod.AudioRecorder()
    rec.start()
    rec.discard()

    assert opened == ([12] if wasapi_available else [12, None])


def test_stale_input_callback_does_not_pollute_next_recording(fake_sd):
    heard = []
    rec = recorder_mod.AudioRecorder(on_first_frame=lambda: heard.append(True))
    rec.start()
    old = rec._stream
    rec.discard()
    rec.start()
    current = rec._stream

    old.feed(np.full((4, 1), 0.25, dtype=np.float32), 4)
    assert not heard
    current.feed(np.full((4, 1), 0.5, dtype=np.float32), 4)
    np.testing.assert_array_equal(rec.stop(), np.full(4, 0.5, dtype=np.float32))
    assert heard == [True]


def test_warm_stream_only_copies_audio_after_recording_begins(fake_sd) -> None:
    copied_idle: list[bool] = []
    events: list[str] = []

    class IdleAudio:
        def copy(self):
            copied_idle.append(True)
            return np.zeros((1, 1), dtype=np.float32)

    rec = recorder_mod.AudioRecorder(
        max_seconds=1,
        on_first_frame=lambda: events.append("first"),
        on_auto_stop=lambda: events.append("limit"),
    )
    assert rec.set_warm_enabled(True)
    stream = rec._stream
    assert stream is not None and stream.active
    assert not rec.is_recording
    stream.feed(IdleAudio(), 16000)
    assert rec.stop().size == 0
    assert events == copied_idle == []

    rec.start()
    assert rec.is_recording
    assert rec._stream is stream and stream.start_calls == 1
    stream.feed(IdleAudio(), 16000, adc_time=stream.time - 2)
    assert events == copied_idle == []
    data = np.array([[1], [2], [3], [4]], dtype=np.float32)
    stream.feed(data, 4, adc_time=stream.time - 1.75 / 16000)
    np.testing.assert_array_equal(rec.stop(), data[2:, 0])
    assert events == ["first"]
    assert stream.active and not rec.is_recording

    stream.feed(IdleAudio(), 16000)
    assert copied_idle == []
    rec.start()
    stream.feed(data, 4)
    rec.discard()
    assert rec.stop().size == 0
    assert events == ["first", "first"]
    assert stream.active
    rec.close()
    assert not stream.active and stream.close_calls == 1


def test_disabling_warm_stream_while_recording_closes_on_release(fake_sd) -> None:
    rec = recorder_mod.AudioRecorder()
    assert rec.set_warm_enabled(True)
    stream = rec._stream
    rec.start()
    assert not rec.set_warm_enabled(False)
    assert rec.is_recording and stream.active
    stream.feed(np.ones((4, 1), dtype=np.float32), 4)
    np.testing.assert_array_equal(rec.stop(), np.ones(4, dtype=np.float32))
    assert stream.close_calls == 1


def test_switching_device_closes_idle_warm_stream(fake_sd) -> None:
    rec = recorder_mod.AudioRecorder(device_index=1)
    assert rec.set_warm_enabled(True)
    old_stream = rec._stream

    rec.device_index = 2
    new_stream = rec._stream
    assert old_stream.close_calls == 1
    assert new_stream.kwargs["device"] == 2
    assert new_stream.active and not rec.is_recording
    rec.set_warm_enabled(False)
    assert new_stream.close_calls == 1


@pytest.mark.parametrize("failure", ["open", "idle_error"])
def test_unavailable_warm_input_recovers_on_next_recording(monkeypatch, fake_sd, failure) -> None:
    attempts = 0

    def open_stream(**kwargs):
        nonlocal attempts
        attempts += 1
        if failure == "open" and attempts == 1:
            raise recorder_mod.sd.PortAudioError("unavailable")
        return FakeInputStream(**kwargs)

    monkeypatch.setattr(recorder_mod.sd, "InputStream", open_stream)
    errors: list[str] = []
    rec = recorder_mod.AudioRecorder(on_stream_error=errors.append)
    samples = np.ones((4, 1), dtype=np.float32)
    if failure == "idle_error":
        assert rec.set_warm_enabled(True)
        old_stream = rec._stream
        old_stream.feed(samples, 4, status=_make_fatal_status())
        assert not old_stream.active
        assert errors == []
    else:
        assert not rec.set_warm_enabled(True)
        assert rec._stream is None

    rec.start()
    assert attempts == 2
    current_stream = rec._stream
    if failure == "idle_error":
        assert current_stream is not old_stream
        assert old_stream.close_calls == 1
    current_stream.feed(samples, 4)
    np.testing.assert_array_equal(rec.stop(), samples[:, 0])
    rec.close()


def test_stop_without_start_returns_empty(fake_sd) -> None:
    rec = recorder_mod.AudioRecorder()
    out = rec.stop()
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert out.shape == (0,)


def test_start_self_heals_residual_stream(fake_sd) -> None:
    """start 发现上次流未释放（上层状态失步）时：不抛错，强制关闭旧流后继续。

    回归「麦克风永久锁死」bug：旧 worker 收尾把新任务状态踩成 IDLE ->
    handle_hotkey_release 静默 return 不关流 -> AudioRecorder._stream 残留 ->
    之后所有 start 抛 RuntimeError("正在录音，不能重复 start")。修复后 start
    自愈：取走残留引用并置 None，锁外强制 stop+close 旧流，再正常打开新流。
    """
    rec = recorder_mod.AudioRecorder()
    rec.start()
    stale_stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    assert stale_stream.stop_calls == 0
    assert stale_stream.close_calls == 0

    # 制造残留：不 stop，直接再次 start。不再抛 RuntimeError。
    rec.start()

    # 旧流被强制 stop + close
    assert stale_stream.stop_calls == 1
    assert stale_stream.close_calls == 1
    # 新流生效（is_recording=True 且是一个新的 FakeInputStream 实例）
    assert rec.is_recording
    new_stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    assert new_stream is not stale_stream
    rec.stop()


def test_start_self_heal_closes_old_stream_even_if_new_start_raises(monkeypatch) -> None:
    """自愈路径：即便新流 start() 抛异常，残留旧流也必须被关闭（finally 语义）。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeStream)

    def _q(*a, **kw):
        return {"name": "fake-mic", "default_samplerate": 16000.0, "max_input_channels": 1}
    monkeypatch.setattr(recorder_mod.sd, "query_devices", _q)

    rec = recorder_mod.AudioRecorder()
    rec.start()
    stale_stream: FakeStream = rec._stream  # type: ignore[assignment]
    assert stale_stream.close_calls == 0

    # 新构造的流 start() 会抛
    FakeStream.start_raises = True
    try:
        with pytest.raises(RuntimeError):
            rec.start()
    finally:
        FakeStream.start_raises = False

    # 旧流仍被关闭（自愈 finally 保证）
    assert stale_stream.close_calls == 1


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
    assert stream.kwargs["latency"] == "low"
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


def test_default_device_uses_kind_input_not_devicelist(monkeypatch) -> None:
    """device_index=None 时必须用 sd.query_devices(kind="input") 拿默认输入设备 dict，
    而不是 query_devices(None)（后者返回 DeviceList，无 .get，会触发异常回退）。

    回归：之前每次录音开始都报 query_devices(device_index=None) 失败的 AttributeError
    并静默回退 16kHz；修复后 None 路径走 kind="input" 并正常返回真实采样率。
    """
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeInputStream)
    calls: list[tuple] = []

    # 模拟真实 sounddevice 语义：
    # - query_devices(None) -> 返回 DeviceList（list 子类，无 .get 属性）
    # - query_devices(kind="input") -> 返回默认输入设备 dict
    class _FakeDeviceList(list):
        """模拟 sounddevice.DeviceList：是 list 但没有 dict 的 .get 方法。"""

    def _q(device=None, *args, **kwargs):
        calls.append((device, args, kwargs))
        if "kind" in kwargs:
            # kind="input" 路径：返回默认输入设备 dict（含真实采样率 48k）。
            return {"name": "default-input", "default_samplerate": 48000.0,
                    "max_input_channels": 1}
        if device is None:
            # 与真实 sounddevice 行为一致：query_devices(None) 返回 DeviceList。
            return _FakeDeviceList([
                {"name": "Mic A", "max_input_channels": 1, "default_samplerate": 48000.0},
            ])
        return {"name": "x", "default_samplerate": 16000.0, "max_input_channels": 1}

    monkeypatch.setattr(recorder_mod.sd, "query_devices", _q)
    rec = recorder_mod.AudioRecorder()  # device_index=None
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 必须调用了 kind="input" 路径（拿回 48k），且不能走过异常回退（=16k）。
    assert any(c[2].get("kind") == "input" for c in calls), \
        f"未调用 query_devices(kind='input')；calls={calls}"
    assert stream.kwargs["samplerate"] == 48000
    rec.stop()


def test_default_device_query_failure_still_falls_back(monkeypatch) -> None:
    """device_index=None 且 query_devices(kind='input') 抛异常时仍回退 16k。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeInputStream)

    def boom(*a, **kw):
        raise RuntimeError("sd unavailable")

    monkeypatch.setattr(recorder_mod.sd, "query_devices", boom)
    rec = recorder_mod.AudioRecorder()  # device_index=None
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


# ==================== on_first_frame 首帧一次性通知 ====================


def test_first_frame_invoked_once_on_first_callback(fake_sd) -> None:
    """start 后首个音频回调触发 on_first_frame 恰好一次。"""
    calls = []
    rec = recorder_mod.AudioRecorder(on_first_frame=lambda: calls.append(1))
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream.feed(np.zeros((4, 1), dtype=np.float32), frames=4)
    stream.feed(np.zeros((4, 1), dtype=np.float32), frames=4)
    stream.feed(np.zeros((4, 1), dtype=np.float32), frames=4)
    assert len(calls) == 1
    rec.stop()


def test_first_frame_not_invoked_without_callback_set(fake_sd) -> None:
    """不传 on_first_frame 时回调路径仍正常，无副作用。"""
    rec = recorder_mod.AudioRecorder()  # on_first_frame=None
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 不应抛异常
    stream.feed(np.zeros((4, 1), dtype=np.float32), frames=4)
    assert not rec._first_frame_pending
    rec.stop()


def test_first_frame_re_arms_on_new_start(fake_sd) -> None:
    """stop 后再次 start 会再次触发 on_first_frame。"""
    calls = []
    rec = recorder_mod.AudioRecorder(on_first_frame=lambda: calls.append(1))
    rec.start()
    stream1: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream1.feed(np.zeros((4, 1), dtype=np.float32), frames=4)
    assert len(calls) == 1
    rec.stop()

    rec.start()
    stream2: FakeInputStream = rec._stream  # type: ignore[assignment]
    stream2.feed(np.zeros((4, 1), dtype=np.float32), frames=4)
    assert len(calls) == 2
    rec.stop()


def test_first_frame_not_invoked_after_stop(fake_sd) -> None:
    """stop 后挂起的回调不再触发 on_first_frame（pending 已重置）。"""
    calls = []
    rec = recorder_mod.AudioRecorder(on_first_frame=lambda: calls.append(1))
    rec.start()
    # 直接 stop，未喂任何帧
    rec.stop()
    assert rec._first_frame_pending is False

    # 即便手动对已停止的 stream 喂回调，也不应触发（pending=False）
    # 注意：rec._stream 已置 None，但旧 stream 引用的 callback 仍是 rec._callback；
    # 这里直接调 rec._callback 模拟迟到帧。
    rec._callback(np.zeros((4, 1), dtype=np.float32), 4, 0, 0)
    assert calls == []


def test_first_frame_not_invoked_after_discard(fake_sd) -> None:
    """discard 后挂起的回调不再触发 on_first_frame。"""
    calls = []
    rec = recorder_mod.AudioRecorder(on_first_frame=lambda: calls.append(1))
    rec.start()
    rec.discard()
    assert rec._first_frame_pending is False
    rec._callback(np.zeros((4, 1), dtype=np.float32), 4, 0, 0)
    assert calls == []


def test_first_frame_callback_exception_does_not_kill_audio(fake_sd) -> None:
    """on_first_frame 回调抛异常时不应影响后续音频帧处理（异常只记日志）。"""
    def boom() -> None:
        raise RuntimeError("boom")

    rec = recorder_mod.AudioRecorder(on_first_frame=boom)
    rec.start()
    stream: FakeInputStream = rec._stream  # type: ignore[assignment]
    # 首帧回调抛异常，但帧仍应被 append
    stream.feed(np.full((4, 1), 0.5, dtype=np.float32), frames=4)
    # 第二帧也应正常处理（音频线程未被打死）
    stream.feed(np.full((6, 1), 0.6, dtype=np.float32), frames=6)
    out = rec.stop()
    assert out.shape == (10,)
    np.testing.assert_allclose(out[:4], 0.5)
    np.testing.assert_allclose(out[4:], 0.6)


# ==================== stop()/discard() 关流拆分 try ====================


def test_stop_calls_close_even_if_stream_stop_raises(monkeypatch) -> None:
    """stop() 中底层 stream.stop() 抛异常时 close 仍被调用（拆分 try 回归）。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeStream)

    def _q(*a, **kw):
        return {"name": "fake-mic", "default_samplerate": 16000.0, "max_input_channels": 1}
    monkeypatch.setattr(recorder_mod.sd, "query_devices", _q)

    rec = recorder_mod.AudioRecorder()
    rec.start()
    stream: FakeStream = rec._stream  # type: ignore[assignment]
    # 喂一帧保证 stop 走非空分支
    stream.callback(np.zeros((4, 1), dtype=np.float32), 4, 0, 0)

    FakeStream.stop_raises = True
    try:
        out = rec.stop()  # 不应抛错
    finally:
        FakeStream.stop_raises = False
    # stop 抛错仍尝试 close
    assert stream.close_calls == 1
    # stop 异常不影响已录制数据返回
    assert out.shape == (4,)


def test_discard_calls_close_even_if_stream_stop_raises(monkeypatch) -> None:
    """discard() 中底层 stream.stop() 抛异常时 close 仍被调用（拆分 try 回归）。"""
    monkeypatch.setattr(recorder_mod.sd, "InputStream", FakeStream)

    def _q(*a, **kw):
        return {"name": "fake-mic", "default_samplerate": 16000.0, "max_input_channels": 1}
    monkeypatch.setattr(recorder_mod.sd, "query_devices", _q)

    rec = recorder_mod.AudioRecorder()
    rec.start()
    stream: FakeStream = rec._stream  # type: ignore[assignment]

    FakeStream.stop_raises = True
    try:
        rec.discard()  # 不应抛错
    finally:
        FakeStream.stop_raises = False
    assert stream.close_calls == 1


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
