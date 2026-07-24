"""audio.resample 单元测试（纯 numpy 重采样）。

测试目标：
- 48k -> 16k：输出长度按 up/down 比例缩短（约 1/3）。
- 16k -> 16k：相同采样率原样返回（identity）。
- 单一来源：asr/sensevoice_onnx/audio.py 的 resample_audio 与本模块同一函数对象。
"""

from __future__ import annotations

import numpy as np

from dictashift.audio.resample import numpy_resample_poly, resample_audio


def test_resample_48k_to_16k_length_ratio() -> None:
    """48k -> 16k 长度约 1/3（ceil 比例）。"""
    sr = 48000
    target = 16000
    # 0.1 秒 48k 信号 = 4800 采样
    t = np.arange(4800) / sr
    audio = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out = resample_audio(audio, sr, target)
    assert out.dtype == np.float32
    assert out.ndim == 1
    # 长度应为 ceil(4800 * 16000 / 48000) = 1600，允许 ±2 抖动
    assert abs(out.shape[0] - 1600) <= 2


def test_resample_16k_to_16k_identity() -> None:
    """相同采样率 -> 原样返回（同一对象，不重采样）。"""
    audio = np.linspace(-1, 1, 1600, dtype=np.float32)
    out = resample_audio(audio, 16000, 16000)
    # 同采样率：resample_audio 直接返回 audio（identity）
    assert out is audio


def test_resample_poly_up_down_reduction() -> None:
    """numpy_resample_poly 内部对 up/down 约分：4/2 == 2/1。"""
    x = np.arange(100, dtype=np.float32)
    # 同比例两套参数应得到相近结果（约分后等价）
    y1 = numpy_resample_poly(x, 4, 2)
    y2 = numpy_resample_poly(x, 2, 1)
    assert y1.shape == y2.shape
    np.testing.assert_allclose(y1, y2, atol=1e-5)


def test_resample_single_source_asr_module() -> None:
    """asr/sensevoice_onnx/audio.py 导出的 resample_audio 是同一函数对象（单一来源）。"""
    from dictashift.asr.sensevoice_onnx.audio import resample_audio as asr_resample

    assert asr_resample is resample_audio
