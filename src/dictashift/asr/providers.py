from enum import StrEnum
from pathlib import Path


class AsrDevice(StrEnum):
    AUTO = "auto"
    CPU = "cpu"


def resolve_providers(device: AsrDevice, device_id: int = 0) -> list:
    """
    将 AsrDevice 解析为 ort 格式的 provider 列表。

    第一版只支持 CPU 推理（int8 模型）：AUTO 与 CPU 均返回
    CPUExecutionProvider。device_id 参数仅为保持调用方接口稳定。
    """
    return ["CPUExecutionProvider"]


def default_model_dir() -> Path:
    """
    返回仓库内 assets/models/sensevoice_onnx 目录。
    src/dictashift/asr/providers.py -> parents[3] 为仓库根。
    """
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "assets" / "models" / "sensevoice_onnx"


def model_paths(model_dir: Path):
    """
    返回 (encoder_path, decoder_path, tokenizer_path)。
    模型文件名固定 int8（第一版只支持 CPU 推理）。
    """
    encoder_path = model_dir / "SenseVoice-Encoder.int8.onnx"
    decoder_path = model_dir / "SenseVoice-CTC.int8.onnx"
    tokenizer_path = model_dir / "Tokenizer.bpe.model"
    return encoder_path, decoder_path, tokenizer_path
