"""ASR 引擎纯 NumPy 逻辑测试（不加载真实模型）。

测试目标：
- NumPyMelExtractor：1 秒 16kHz 随机噪声 -> extract 输出 shape (T, 560)、dtype float32、无 NaN。
- ResultIntegrator.integrate：无热词时 greedy 直通；热词区间替换语义（构造简单用例）。
- engine._merge_results：两片有重叠文本的结果合并后无重复（用 fake TranscriptionResult 构造）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from dictashift.asr.sensevoice_onnx.audio import NumPyMelExtractor
from dictashift.asr.sensevoice_onnx.engine import SenseVoiceInference
from dictashift.asr.sensevoice_onnx.integrator import ResultIntegrator
from dictashift.asr.sensevoice_onnx.schema import (
    RecognitionResult,
    Timings,
    TranscriptionResult,
)


# ==================== NumPyMelExtractor ====================


def test_mel_extractor_output_shape_dtype_no_nan() -> None:
    """1 秒 16kHz 随机噪声 -> (T, 560)、float32、无 NaN。"""
    fe = NumPyMelExtractor(sr=16000)
    audio = (np.random.randn(16000).astype(np.float32) * 0.01)
    feat = fe.extract(audio)
    assert feat.ndim == 2
    assert feat.shape[1] == 560
    assert feat.dtype == np.float32
    assert not np.isnan(feat).any()
    assert feat.shape[0] > 0


def test_mel_extractor_t_frame_count() -> None:
    """T_lfr = (T_mel + 5) // 6；1 秒音频应产生正数帧数。"""
    fe = NumPyMelExtractor(sr=16000)
    feat = fe.extract(np.zeros(16000, dtype=np.float32))
    # 1 秒约 100 mel 帧 -> T_lfr ≈ (100+5)//6 ≈ 17
    assert 10 <= feat.shape[0] <= 30


def test_mel_extractor_short_input_no_crash() -> None:
    """极短输入不崩溃（边界）。"""
    fe = NumPyMelExtractor(sr=16000)
    feat = fe.extract(np.zeros(800, dtype=np.float32))
    assert feat.shape[1] == 560


# ==================== ResultIntegrator.integrate ====================


def test_integrate_no_hotwords_passthrough() -> None:
    """无热词时 greedy 直通（输出 = greedy，is_hotword=False）。"""
    greedy = [
        {"text": "你", "start": 0.0},
        {"text": "好", "start": 0.1},
        {"text": "世", "start": 0.2},
        {"text": "界", "start": 0.3},
    ]
    result = ResultIntegrator.integrate(greedy, [])
    assert [r["text"] for r in result] == ["你", "好", "世", "界"]
    assert all(r.get("is_hotword") is False for r in result)


def test_integrate_hotword_replaces_span() -> None:
    """热词区间替换语义：落在热词时间区间内的 greedy token 被热词文本替换。"""
    greedy = [
        {"text": "a", "start": 0.0},
        {"text": "b", "start": 0.1},   # 落在热词 [0.05, 0.25] 内
        {"text": "c", "start": 0.2},   # 落在热词区间内
        {"text": "d", "start": 0.4},   # 在热词之后
    ]
    hotwords = [
        {
            "text": "HOT",
            "start": 0.05,
            "end": 0.25,
            "tokens": [{"token": "hot", "time": 0.05}],
        }
    ]
    result = ResultIntegrator.integrate(greedy, hotwords)
    texts = [r["text"] for r in result]
    # 热词替换了区间内的 b/c，前后 greedy a/d 保留
    joined = "".join(texts)
    assert "HOT" in joined
    assert "a" in joined and "d" in joined
    # 热词块标记为 is_hotword=True
    assert any(r.get("is_hotword") is True for r in result)


def test_integrate_empty_inputs() -> None:
    """空输入返回空列表。"""
    assert ResultIntegrator.integrate([], []) == []


# ==================== SenseVoiceInference._merge_results ====================


def _make_result(text: str, items: list[tuple[str, float]]) -> TranscriptionResult:
    """构造 fake TranscriptionResult：text + RecognitionResult 列表。"""
    results = [RecognitionResult(text=t, start=s, is_hotword=False) for t, s in items]
    return TranscriptionResult(
        text=text,
        results=results,
        hotwords=[],
        timings=Timings(),
    )


def test_merge_results_single_returns_same() -> None:
    """单片结果直接返回原对象。"""
    r = _make_result("abc", [("a", 0.0), ("b", 0.1), ("c", 0.2)])
    out = SenseVoiceInference._merge_results(None, [r], 0.5)
    assert out is r


def test_merge_results_empty_returns_none() -> None:
    assert SenseVoiceInference._merge_results(None, [], 0.5) is None


def test_merge_results_overlapping_no_duplicate() -> None:
    """两片有重叠文本的结果合并后无重复（重叠区被 SequenceMatcher 去重）。"""
    # 片1: "hello world"，片2: "world again"（"world" 为重叠）
    r1 = _make_result("hello world", [("hello ", 0.0), ("world", 0.3)])
    r2 = _make_result("world again", [("world", 0.3), (" again", 0.6)])
    out = SenseVoiceInference._merge_results(None, [r1, r2], overlap_sec=0.5)
    assert out is not None
    # 合并后不应出现 "worldworld" 这种重复
    assert "worldworld" not in out.text
    assert "hello" in out.text
    assert "again" in out.text
