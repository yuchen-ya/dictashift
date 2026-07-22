"""ASR providers 与 ASRService 引擎生命周期测试（mock 引擎，不加载真实模型）。

测试目标 (AGENTS.md 5.3 / 13.1)：
- resolve_providers：第一版只支持 CPU，AUTO 与 CPU 均返回 CPUExecutionProvider。
- ASRService（monkeypatch dictashift.asr.service.SenseVoiceInference 为 fake）：
  * initialize(AUTO)：CPU 构建成功 -> actual==CPU；构建失败 -> RuntimeError 且 is_ready 不变。
  * initialize(CPU) 显式失败 -> 抛异常且 is_ready 不变。
  * switch_device 成功 -> 旧引擎 release；失败 -> 旧引擎未 release、actual/user_device 恢复、抛异常。
  * transcribe 未初始化 -> RuntimeError。
"""

from __future__ import annotations

import pytest

from dictashift.asr import providers as providers_mod
from dictashift.asr.providers import AsrDevice, resolve_providers
from dictashift.asr import service as service_mod


# ==================== resolve_providers ====================


def test_resolve_providers_cpu() -> None:
    assert resolve_providers(AsrDevice.CPU) == ["CPUExecutionProvider"]


def test_resolve_providers_auto_equals_cpu() -> None:
    """第一版只支持 CPU：AUTO 与 CPU 行为等价，均返回 CPUExecutionProvider。"""
    assert resolve_providers(AsrDevice.AUTO) == ["CPUExecutionProvider"]
    assert resolve_providers(AsrDevice.AUTO) == resolve_providers(AsrDevice.CPU)


def test_resolve_providers_device_id_ignored() -> None:
    """device_id 不影响 provider 列表（仅保持调用方接口稳定）。"""
    assert resolve_providers(AsrDevice.CPU, device_id=2) == ["CPUExecutionProvider"]


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
        """按 config.providers 推导"生效" provider 名。"""
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


# 通过类属性控制下一个被创建实例的 warmup 行为
FakeEngine._next_warmup_raises = None


@pytest.fixture
def patched_engine(monkeypatch):
    """monkeypatch SenseVoiceInference 为 FakeEngine，并重置记录。"""
    FakeEngine._instances = []
    FakeEngine._next_warmup_raises = None
    monkeypatch.setattr(service_mod, "SenseVoiceInference", FakeEngine)
    return FakeEngine


def _make_service():
    return service_mod.ASRService(model_dir=providers_mod.default_model_dir())


# ==================== initialize(AUTO / CPU) ====================


def test_initialize_auto_cpu_success(patched_engine) -> None:
    """AUTO：CPU 构建成功 -> actual_device==CPU。"""
    svc = _make_service()
    svc.initialize(AsrDevice.AUTO, device_id=0)
    assert svc.is_ready
    assert svc.user_device == AsrDevice.AUTO
    assert svc.actual_device == AsrDevice.CPU
    assert svc.actual_providers == ["CPUExecutionProvider"]
    # 创建的引擎应做了 warmup
    assert FakeEngine._instances[-1].warmup_calls == 1
    assert len(FakeEngine._instances) == 1


def test_initialize_auto_warmup_failure_releases_engine_and_raises(
    patched_engine, monkeypatch
) -> None:
    """AUTO：CPU warmup 失败 -> 失败引擎被 release、抛 RuntimeError、is_ready 不变。"""

    class _Fail(FakeEngine):
        def warmup(self):
            raise RuntimeError("cpu warmup failed")

    monkeypatch.setattr(service_mod, "SenseVoiceInference", _Fail)

    svc = _make_service()
    with pytest.raises(RuntimeError):
        svc.initialize(AsrDevice.AUTO, device_id=0)
    assert not svc.is_ready
    # 失败的引擎实例应被 release
    assert len(FakeEngine._instances) == 1
    assert FakeEngine._instances[0].release_calls == 1


def test_initialize_auto_all_fail_raises(patched_engine, monkeypatch) -> None:
    """AUTO：CPU 失败 -> RuntimeError。"""

    class _AlwaysFail(FakeEngine):
        def warmup(self) -> None:
            raise RuntimeError("cpu build failed")

    monkeypatch.setattr(service_mod, "SenseVoiceInference", _AlwaysFail)

    s = _make_service()
    with pytest.raises(RuntimeError):
        s.initialize(AsrDevice.AUTO, device_id=0)
    assert not s.is_ready


# ==================== initialize(CPU) 显式失败 ====================


def test_initialize_cpu_explicit_failure_raises_and_is_ready_unchanged(
    patched_engine, monkeypatch
) -> None:
    """显式 initialize(CPU) 失败 -> 抛异常且 is_ready 不变。"""
    # 先成功初始化一次，让 is_ready=True
    svc = _make_service()
    svc.initialize(AsrDevice.CPU, device_id=0)
    assert svc.is_ready
    ready_before = svc.is_ready
    old_engine = FakeEngine._instances[-1]

    # 替换为始终失败的引擎
    class _Fail(FakeEngine):
        def warmup(self):
            raise RuntimeError("cpu explicit fail")

    monkeypatch.setattr(service_mod, "SenseVoiceInference", _Fail)

    with pytest.raises(RuntimeError):
        svc.initialize(AsrDevice.CPU, device_id=0)

    # is_ready 保持不变（旧引擎未被破坏）
    assert svc.is_ready == ready_before
    assert old_engine.release_calls == 0


# ==================== switch_device ====================


def test_switch_device_success_releases_old(patched_engine) -> None:
    """switch_device 成功 -> 旧引擎 release 被调用、actual 更新。"""
    svc = _make_service()
    svc.initialize(AsrDevice.CPU, device_id=0)
    old_engine = FakeEngine._instances[-1]
    assert old_engine.warmup_calls == 1
    assert old_engine.release_calls == 0

    svc.switch_device(AsrDevice.CPU, device_id=1)
    # 旧引擎被 release
    assert old_engine.release_calls == 1
    assert svc.actual_device == AsrDevice.CPU
    assert svc.user_device == AsrDevice.CPU


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
        svc.switch_device(AsrDevice.AUTO, device_id=2)

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
