"""ASR providers 与 ASRService 设备切换测试（mock 引擎，不加载真实模型）。

测试目标 (AGENTS.md 5.3 / 13.1)：
- resolve_providers 三种映射；CUDA 列表含 CPU 回退；device_id 传递。
- MODEL_VARIANT：CUDA->fp16、CPU->int8。
- ASRService（monkeypatch dictashift.asr.service.SenseVoiceInference 为 fake）：
  * initialize(AUTO)：CUDA 成功 -> actual==CUDA；CUDA 失败 -> CPU；都失败 -> RuntimeError。
  * initialize(CUDA) 显式失败 -> 抛异常且 is_ready 不变。
  * switch_device 成功 -> 旧引擎 release；失败 -> 旧引擎未 release、actual/user_device 恢复、抛异常。
  * transcribe 未初始化 -> RuntimeError。
"""

from __future__ import annotations

import pytest

from dictashift.asr import providers as providers_mod
from dictashift.asr.providers import AsrDevice, MODEL_VARIANT, resolve_providers
from dictashift.asr import service as service_mod


# ==================== resolve_providers / MODEL_VARIANT ====================


def test_resolve_providers_cpu() -> None:
    assert resolve_providers(AsrDevice.CPU) == ["CPUExecutionProvider"]


def test_resolve_providers_cuda_has_cpu_fallback_and_device_id() -> None:
    providers = resolve_providers(AsrDevice.CUDA, device_id=2)
    assert providers[0] == ("CUDAExecutionProvider", {"device_id": 2})
    assert providers[-1] == "CPUExecutionProvider"
    assert len(providers) == 2


def test_resolve_providers_cuda_default_device_id() -> None:
    providers = resolve_providers(AsrDevice.CUDA)
    assert providers[0] == ("CUDAExecutionProvider", {"device_id": 0})


def test_resolve_providers_auto_empty() -> None:
    """AUTO 不做静态解析（由 ASRService 运行时探测）。"""
    assert resolve_providers(AsrDevice.AUTO) == []


def test_model_variant_mapping() -> None:
    assert MODEL_VARIANT[AsrDevice.CUDA] == "fp16"
    assert MODEL_VARIANT[AsrDevice.CPU] == "int8"


# ==================== Fake SenseVoiceInference ====================


class FakeEngine:
    """记录 config、可控 warmup 成败、recognize 返回固定结果的 fake 引擎。"""

    # 类级记录所有实例，便于断言 release 调用
    _instances: list["FakeEngine"] = []

    def __init__(self, config) -> None:
        self.config = config
        self.warmup_calls = 0
        self.recognize_calls = 0
        self.release_calls = 0
        self._warmup_raises = FakeEngine._next_warmup_raises
        FakeEngine._instances.append(self)

    def active_providers(self) -> list[str]:
        """按 config.providers 推导"生效" provider 名；可被 _next_active_providers 覆盖。"""
        if FakeEngine._next_active_providers is not None:
            effective = FakeEngine._next_active_providers
            FakeEngine._next_active_providers = None
            return effective
        names: list[str] = []
        for p in self.config.providers:
            name = p[0] if isinstance(p, tuple) else p
            if name not in names:
                names.append(name)
        return names

    def warmup(self) -> None:
        self.warmup_calls += 1
        if self._warmup_raises is not None:
            exc = self._warmup_raises
            # 清除，避免影响后续实例
            FakeEngine._next_warmup_raises = None
            raise exc

    def recognize(self, audio, lid="auto", itn=True, **kw):
        self.recognize_calls += 1
        # 返回带 .text 的简单对象
        class _R:
            text = "fake"
        return _R()

    def release(self) -> None:
        self.release_calls += 1


# 通过类属性控制下一个被创建实例的 warmup 行为，模拟"CUDA 构建抛异常"
FakeEngine._next_warmup_raises = None
# 控制下一个实例 active_providers 的返回值，模拟"CUDA 静默回退 CPU"
FakeEngine._next_active_providers = None


@pytest.fixture
def patched_engine(monkeypatch):
    """monkeypatch SenseVoiceInference 为 FakeEngine，并重置记录。"""
    FakeEngine._instances = []
    FakeEngine._next_warmup_raises = None
    FakeEngine._next_active_providers = None
    monkeypatch.setattr(service_mod, "SenseVoiceInference", FakeEngine)
    return FakeEngine


def _make_service():
    return service_mod.ASRService(model_dir=providers_mod.default_model_dir())


# ==================== initialize(AUTO) ====================


def test_initialize_auto_cuda_success(patched_engine) -> None:
    """AUTO：CUDA 构建成功 -> actual_device==CUDA。"""
    svc = _make_service()
    svc.initialize(AsrDevice.AUTO, device_id=0)
    assert svc.is_ready
    assert svc.user_device == AsrDevice.AUTO
    assert svc.actual_device == AsrDevice.CUDA
    # 创建的引擎应做了 warmup
    assert FakeEngine._instances[-1].warmup_calls == 1


def test_initialize_auto_cuda_fails_falls_back_cpu(patched_engine) -> None:
    """AUTO：CUDA 构建抛异常 -> 落 CPU 且 actual_device==CPU。"""
    # 让第一个被创建实例（CUDA）warmup 抛异常，CPU 实例正常
    FakeEngine._next_warmup_raises = RuntimeError("cuda build failed")
    svc = _make_service()
    svc.initialize(AsrDevice.AUTO, device_id=0)
    assert svc.is_ready
    assert svc.actual_device == AsrDevice.CPU
    # 应创建了两个引擎实例（CUDA 失败 + CPU 成功）
    assert len(FakeEngine._instances) == 2


def test_initialize_auto_all_fail_raises(patched_engine) -> None:
    """AUTO：CUDA 与 CPU 都失败 -> RuntimeError。"""
    # 让前两个实例都抛异常（CUDA 与 CPU）
    import itertools

    counter = {"n": 0}

    class _AlwaysFail(FakeEngine):
        def warmup(self) -> None:
            counter["n"] += 1
            raise RuntimeError(f"fail #{counter['n']}")

    patched_engine._instances = []
    import dictashift.asr.service as svc_mod
    # 直接替换为始终失败的类
    import sys
    orig = svc_mod.SenseVoiceInference
    svc_mod.SenseVoiceInference = _AlwaysFail
    try:
        s = _make_service()
        with pytest.raises(RuntimeError):
            s.initialize(AsrDevice.AUTO, device_id=0)
        assert not s.is_ready
    finally:
        svc_mod.SenseVoiceInference = orig


def test_initialize_auto_cuda_silent_fallback_detected(patched_engine) -> None:
    """AUTO：CUDA 创建/warmup 未抛错但 get_providers 无 CUDA（静默回退）-> 判定失败落 CPU。

    对应 onnxruntime 在 cuDNN 缺失时只警告不抛异常的真实行为 (AGENTS.md 5.3)。
    """
    # 第一个实例（CUDA）warmup 通过，但生效 providers 只有 CPU
    FakeEngine._next_active_providers = ["CPUExecutionProvider"]
    svc = _make_service()
    svc.initialize(AsrDevice.AUTO, device_id=0)
    assert svc.is_ready
    assert svc.actual_device == AsrDevice.CPU
    assert svc.actual_providers == ["CPUExecutionProvider"]
    # CUDA 假成功的引擎应被 release，共创建两个实例
    assert len(FakeEngine._instances) == 2
    assert FakeEngine._instances[0].release_calls == 1


def test_initialize_cuda_explicit_silent_fallback_raises(patched_engine) -> None:
    """显式 CUDA：静默回退（生效 providers 无 CUDA）-> 抛异常且不破坏旧引擎。"""
    svc = _make_service()
    svc.initialize(AsrDevice.CPU, device_id=0)
    old_engine = FakeEngine._instances[-1]

    FakeEngine._next_active_providers = ["CPUExecutionProvider"]
    with pytest.raises(RuntimeError, match="CUDAExecutionProvider 未生效"):
        svc.initialize(AsrDevice.CUDA, device_id=0)

    assert svc.actual_device == AsrDevice.CPU
    assert old_engine.release_calls == 0


# ==================== initialize(CUDA) 显式失败 ====================


def test_initialize_cuda_explicit_failure_raises_and_is_ready_unchanged(
    patched_engine, monkeypatch
) -> None:
    """显式 initialize(CUDA) 失败 -> 抛异常且 is_ready 不变。"""
    # 先成功初始化一次（CPU），让 is_ready=True
    svc = _make_service()
    svc.initialize(AsrDevice.CPU, device_id=0)
    assert svc.is_ready
    ready_before = svc.is_ready

    # 替换为始终失败的引擎
    class _Fail(FakeEngine):
        def warmup(self):
            raise RuntimeError("cuda explicit fail")

    monkeypatch.setattr(service_mod, "SenseVoiceInference", _Fail)

    with pytest.raises(RuntimeError):
        svc.initialize(AsrDevice.CUDA, device_id=0)

    # is_ready 保持不变（旧引擎未被破坏）
    assert svc.is_ready == ready_before


# ==================== switch_device ====================


def test_switch_device_success_releases_old(patched_engine) -> None:
    """switch_device 成功 -> 旧引擎 release 被调用、actual 更新。"""
    svc = _make_service()
    svc.initialize(AsrDevice.CPU, device_id=0)
    old_engine = FakeEngine._instances[-1]
    assert old_engine.warmup_calls == 1
    assert old_engine.release_calls == 0

    svc.switch_device(AsrDevice.CUDA, device_id=1)
    # 旧引擎被 release
    assert old_engine.release_calls == 1
    assert svc.actual_device == AsrDevice.CUDA
    assert svc.user_device == AsrDevice.CUDA


def test_switch_device_failure_preserves_old(patched_engine, monkeypatch) -> None:
    """switch_device 失败 -> 旧引擎未被 release、actual/user_device 恢复原值、抛异常。"""
    svc = _make_service()
    svc.initialize(AsrDevice.CPU, device_id=0)
    old_engine = FakeEngine._instances[-1]
    user_before = svc.user_device
    actual_before = svc.actual_device

    # 替换为失败引擎
    class _Fail(FakeEngine):
        def warmup(self):
            raise RuntimeError("switch build fail")

    monkeypatch.setattr(service_mod, "SenseVoiceInference", _Fail)

    with pytest.raises(RuntimeError):
        svc.switch_device(AsrDevice.CUDA, device_id=2)

    # 旧引擎未被 release
    assert old_engine.release_calls == 0
    # actual / user_device 恢复原值
    assert svc.user_device == user_before
    assert svc.actual_device == actual_before
    assert svc.is_ready


# ==================== transcribe 未初始化 ====================


def test_transcribe_uninitialized_raises_runtime_error(patched_engine) -> None:
    """transcribe 未初始化 -> RuntimeError。"""
    import numpy as np

    svc = _make_service()
    with pytest.raises(RuntimeError):
        svc.transcribe(np.zeros(1600, dtype=np.float32))
