"""音频重采样工具（纯 numpy 实现）。

历史上 numpy_resample_poly / resample_audio 与 NumPyMelExtractor 一起放在
asr/sensevoice_onnx/audio.py 中。现上移到 audio/ 包，作为整条音频管线的
单一来源（recorder 需要 48k/44.1k -> 16k 重采样，ASR 前端也复用同一实现）。

asr/sensevoice_onnx/audio.py 通过相对导入复用本模块，不复制实现。
"""

from __future__ import annotations

import math

import numpy as np


# 输出分块大小（采样数）。直接一次性构造 (length_out, K_max) 的 gather 矩阵
# 在 60s 录音下会占几百 MB 内存，分块后峰值约 30MB。
_OUT_CHUNK = 65536


def numpy_resample_poly(x, up, down, window_size=10):
    """
    纯 numpy 实现的 resample_poly（高效多相 gather 实现）。

    数学上与 scipy.signal.resample_poly 等价（滤波器设计保持一致），
    但不构造上采样后的长数组做全卷积——旧实现对 44.1k->16k 会把
    8 秒音频上采样 160 倍到 5600 万点再 np.convolve（8821 taps），
    实测单次要 27 秒，是"说完话卡死"的根因。

    推导：y[m] = Σ_j x[j]·h[offset + m·down − j·up]。
    令 N = offset + m·down，r = N % up，则 h 的下标必为 r, r+up, r+2up…，
    即 y[m] = Σ_k x[N//up − k] · h[r + k·up]。
    对每个输出分块构造 (chunk, K_max) 的采样 gather 矩阵与相位滤波器矩阵，
    逐元素相乘后按行求和，复杂度 O(length_out × n_taps/up)。
    """
    # 1. 约分
    g = math.gcd(up, down)
    up //= g
    down //= g

    if up == down:
        return x.copy()

    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if len(x) == 0:
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
    h = h.astype(np.float32)

    # 3. 多相 gather：y[m] = Σ_k x_pad[j0(m) + (K_max-1-k)] · H[m, k]
    length_in = len(x)
    length_out = int(math.ceil(length_in * up / down))
    offset = half_len  # = (n_taps - 1) // 2

    k_max = int(math.ceil(n_taps / up))  # 各相位滤波器的最大 taps 数
    # 左右各 pad (k_max-1)，保证 j0-k 的负下标/越界下标都落到 0 采样上。
    x_pad = np.concatenate(
        [np.zeros(k_max - 1, dtype=np.float32), x, np.zeros(k_max - 1, dtype=np.float32)]
    )

    out = np.empty(length_out, dtype=np.float32)
    for start in range(0, length_out, _OUT_CHUNK):
        m = np.arange(start, min(start + _OUT_CHUNK, length_out), dtype=np.int64)
        n_vec = offset + m * down
        r = n_vec % up
        j0 = n_vec // up  # = (N - r) / up

        # gather 下标：x_pad[j0 - k + (k_max-1)]，k = 0..k_max-1
        idx = j0[:, None] - np.arange(k_max, dtype=np.int64)[None, :] + (k_max - 1)
        # 相位滤波器矩阵：H[m, k] = h[r + k*up]，越界（>= n_taps）补 0
        h_idx = r[:, None] + np.arange(k_max, dtype=np.int64)[None, :] * up
        h_mat = np.where(h_idx < n_taps, h[np.clip(h_idx, 0, n_taps - 1)], 0.0)

        out[start : start + len(m)] = np.einsum("mk,mk->m", x_pad[idx], h_mat)

    return out


def resample_audio(audio, sr, target_sr):
    """音频重采样封装"""
    if sr == target_sr:
        return audio
    return numpy_resample_poly(audio, target_sr, sr)
