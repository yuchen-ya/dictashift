import threading
import logging
from pathlib import Path

import numpy as np

from .providers import (
    AsrDevice,
    MODEL_VARIANT,
    default_model_dir,
    resolve_providers,
    model_paths,
)
from .sensevoice_onnx import SenseVoiceInference, ASREngineConfig, TranscriptionResult

logger = logging.getLogger(__name__)


class ASRService:
    """
    ASR 服务：负责设备探测、引擎生命周期与线程安全的推理调度。

    不变量：
    1. AUTO 流程：先尝试 CUDA 创建引擎 + warmup，任何异常则落回 CPU 创建 + warmup；
       都失败则抛出带原始错误信息的 RuntimeError。
    2. 显式 CUDA：创建或 warmup 失败必须抛出异常且不破坏当前可用引擎。
    3. switch_device：在没有正在进行的 transcribe 时，先完整构建新引擎 (含 warmup)，
       成功后用锁原子替换旧引擎并 release 旧引擎；失败时保留旧引擎、
       user_device 恢复原值、异常抛给调用方。
    4. 用户设置 (user_device) 与实际运行 (actual_device / actual_providers) 分开记录。
    5. 引擎构建 (含 warmup) 可能耗时，由调用方负责放到后台线程；
       ASRService 自身用 threading.Lock 保证 transcribe 与 switch 互斥。
    6. 线程安全：transcribe 持有锁期间调用引擎；引擎不可用时抛 RuntimeError。
    """

    def __init__(self, model_dir: Path | None = None):
        self._model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self._lock = threading.Lock()
        self._engine: SenseVoiceInference | None = None
        self._user_device: AsrDevice | None = None
        self._user_device_id: int = 0
        self._actual_device: AsrDevice | None = None
        self._actual_providers: list[str] = []

    # ==================== 只读属性 ====================

    @property
    def user_device(self) -> AsrDevice:
        """用户设置的设备 (未初始化时为 None)。"""
        return self._user_device

    @property
    def actual_device(self) -> AsrDevice | None:
        """实际运行设备 (AUTO 解析后的结果)。"""
        return self._actual_device

    @property
    def actual_providers(self) -> list[str]:
        """实际 Session providers。"""
        return list(self._actual_providers)

    @property
    def is_ready(self) -> bool:
        return self._engine is not None

    # ==================== 引擎构建 ====================

    def _build_engine(self, device: AsrDevice, device_id: int) -> SenseVoiceInference:
        """
        根据 device 构建并 warmup 一个新引擎。失败时抛出异常。
        返回的引擎已通过 warmup 验证 Session 真实可用。
        """
        variant = MODEL_VARIANT[device]
        model_dir = self._model_dir
        encoder_path, decoder_path, tokenizer_path = model_paths(model_dir, variant)
        providers = resolve_providers(device, device_id=device_id)
        config = ASREngineConfig(
            encoder_path=str(encoder_path),
            decoder_path=str(decoder_path),
            tokenizer_path=str(tokenizer_path),
            providers=providers,
        )
        engine = SenseVoiceInference(config)
        engine.warmup()
        return engine

    def _resolve_auto(self, device_id: int) -> tuple[SenseVoiceInference, AsrDevice, list]:
        """
        AUTO 解析：先 CUDA 创建 + warmup，失败则 CPU 创建 + warmup；
        都失败则抛出带原始错误信息的 RuntimeError。

        返回 (engine, actual_device, actual_providers)。
        """
        cuda_error: Exception | None = None
        try:
            engine = self._build_engine(AsrDevice.CUDA, device_id)
            providers = resolve_providers(AsrDevice.CUDA, device_id=device_id)
            return engine, AsrDevice.CUDA, providers
        except Exception as e:  # noqa: BLE001 - 设备探测需捕获任意失败
            cuda_error = e
            logger.warning(f"[ASRService] AUTO 探测 CUDA 失败，落回 CPU: {e!r}")

        try:
            engine = self._build_engine(AsrDevice.CPU, device_id)
            providers = resolve_providers(AsrDevice.CPU, device_id=device_id)
            return engine, AsrDevice.CPU, providers
        except Exception as cpu_error:  # noqa: BLE001
            raise RuntimeError(
                f"ASR 引擎初始化失败 (AUTO)：CUDA 失败={cuda_error!r}；CPU 失败={cpu_error!r}"
            ) from cpu_error

    # ==================== 生命周期 ====================

    def initialize(self, device: AsrDevice = AsrDevice.AUTO, device_id: int = 0) -> None:
        with self._lock:
            engine, actual_device, actual_providers = self._create_for(device, device_id)
            old = self._engine
            self._engine = engine
            self._user_device = device
            self._user_device_id = device_id
            self._actual_device = actual_device
            self._actual_providers = actual_providers
            if old is not None:
                old.release()

    def _create_for(self, device: AsrDevice, device_id: int):
        """
        根据用户设置创建引擎。AUTO 走运行时探测；显式 CPU/CUDA 失败直接抛出。
        不触碰现有 self._engine，调用方负责原子替换。
        """
        if device == AsrDevice.AUTO:
            return self._resolve_auto(device_id)
        engine = self._build_engine(device, device_id)
        providers = resolve_providers(device, device_id=device_id)
        return engine, device, providers

    def switch_device(self, device: AsrDevice, device_id: int = 0) -> None:
        """
        切换设备。锁保证不会与正在进行的 transcribe 并发。
        成功后原子替换并 release 旧引擎；失败时保留旧引擎、user_device 恢复原值。
        """
        with self._lock:
            prev_user_device = self._user_device
            prev_user_device_id = self._user_device_id
            try:
                engine, actual_device, actual_providers = self._create_for(device, device_id)
            except Exception:
                # 失败时保留旧引擎、user_device 恢复原值，异常抛给调用方
                self._user_device = prev_user_device
                self._user_device_id = prev_user_device_id
                raise
            old = self._engine
            self._engine = engine
            self._user_device = device
            self._user_device_id = device_id
            self._actual_device = actual_device
            self._actual_providers = actual_providers
            if old is not None:
                old.release()

    def transcribe(self, audio: np.ndarray, lid: str = "auto", itn: bool = True) -> TranscriptionResult:
        with self._lock:
            if self._engine is None:
                raise RuntimeError("ASR 引擎不可用，请先调用 initialize()。")
            return self._engine.recognize(audio, lid=lid, itn=itn)

    def release(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.release()
                self._engine = None
            self._actual_device = None
            self._actual_providers = []
