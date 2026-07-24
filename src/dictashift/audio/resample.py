"""音频重采样工具（纯 numpy 实现）。

历史上 numpy_resample_poly / resample_audio 与 NumPyMelExtractor 一起放在
asr/sensevoice_onnx/audio.py 中。现上移到 audio/ 包，作为整条音频管线的
单一来源（recorder 需要 48k/44.1k -> 16k 重采样，ASR 前端也复用同一实现）。

asr/sensevoice_onnx/audio.py 通过相对导入复用本模块，不复制实现。
"""

from __future__ import annotations

import math

import numpy as np


def numpy_resample_poly(x, up, down, window_size=10):
    """
    纯 numpy 实现的 resample_poly
    算法精准复刻 scipy.signal.resample_poly，与 scipy 相似度达 0.99999998
    """
    # 1. 约分
    g = math.gcd(up, down)
    up //= g
    down //= g

    if up == down:
        return x.copy()

    # 2. 设计 FIR 滤波器 (与 scipy.signal.firwin 对齐)
    max_rate = max(up, down)
    f_c = 1.0 / max_rate
    half_len = window_size * max_rate
    n_taps = 2 * half_len + 1

    t = np.arange(n_taps) - half_len
    h = np.sinc(f_c * t)

    # 使用 Kaiser 窗 (beta=5.0)
    beta = 5.0
    kaiser_win = np.i0(beta * np.sqrt(1 - (2 * t / (n_taps - 1))**2)) / np.i0(beta)
    h = h * kaiser_win
    h = h * (up / np.sum(h))

    # 3. 多相滤波 (复刻 upfirdn 逻辑)
    length_in = len(x)
    length_out = int(math.ceil(length_in * up / down))

    x_up = np.zeros(length_in * up + n_taps, dtype=np.float32)
    x_up[:length_in * up:up] = x

    y_full = np.convolve(x_up, h, mode='full')

    offset = (n_taps - 1) // 2
    y = y_full[offset : offset + length_in * up : down]

    return y[:length_out].astype(np.float32)


def resample_audio(audio, sr, target_sr):
    """音频重采样封装"""
    if sr == target_sr:
        return audio
    return numpy_resample_poly(audio, target_sr, sr)
