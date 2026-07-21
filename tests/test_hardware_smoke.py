"""硬件冒烟测试（默认被 addopts `-m 'not hardware'` 排除）。

测试目标 (AGENTS.md 13 / 13.1)：
- 用真实 ASRService.initialize(CPU) + 1 秒噪声 transcribe 不崩溃即算过。
- 模型文件缺失时 pytest.skip 并说明原因。
- 必须区分"未配置/不可用"与"实现失败"。

运行方式：`uv run pytest -m hardware` 或 `uv run pytest tests/test_hardware_smoke.py -m hardware`。
"""

from __future__ import annotations

import platform

import numpy as np
import pytest

from dictashift.asr.providers import AsrDevice, default_model_dir
from dictashift.asr.service import ASRService

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        platform.system() != "Windows",
        reason="DictaShift 仅在 Windows 上运行",
    ),
]


def _model_files_present() -> bool:
    """检查 int8 variant 的三个模型文件是否存在。"""
    d = default_model_dir()
    enc = d / "SenseVoice-Encoder.int8.onnx"
    dec = d / "SenseVoice-CTC.int8.onnx"
    tok = d / "Tokenizer.bpe.model"
    return enc.exists() and dec.exists() and tok.exists()


def test_asr_service_initialize_cpu_and_transcribe_no_crash() -> None:
    """真实 ASRService.initialize(CPU) + 1s 噪声 transcribe 不崩溃。

    模型文件缺失时跳过（区分"未配置"与"实现失败"）。
    """
    if not _model_files_present():
        pytest.skip("模型文件缺失（assets/models/sensevoice_onnx/*.int8.onnx），跳过硬件冒烟测试")

    svc = ASRService(model_dir=default_model_dir())
    try:
        svc.initialize(AsrDevice.CPU, device_id=0)
    except Exception as e:
        # 真实初始化失败属于"实现失败"，不跳过，让测试失败暴露问题
        pytest.fail(f"ASR initialize(CPU) 失败（实现失败，非未配置）：{e!r}")

    assert svc.is_ready, "initialize 成功后 is_ready 应为 True"
    assert svc.actual_device == AsrDevice.CPU

    audio = (np.random.randn(16000).astype(np.float32) * 0.01)
    try:
        result = svc.transcribe(audio, lid="auto", itn=True)
    except Exception as e:
        pytest.fail(f"transcribe 1s 噪声失败（实现失败）：{e!r}")
    # 结果对象应有 .text 属性（字符串）
    assert hasattr(result, "text")
    assert isinstance(result.text, str)
