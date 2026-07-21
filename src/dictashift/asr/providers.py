from enum import StrEnum
from pathlib import Path


class AsrDevice(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"


MODEL_VARIANT = {
    AsrDevice.CUDA: "fp16",
    AsrDevice.CPU: "int8",
}


def resolve_providers(device: AsrDevice, device_id: int = 0) -> list:
    """
    将 AsrDevice 解析为 ort 格式的 provider 列表。
    AUTO 不做静态解析，由 ASRService 运行时探测。
    """
    if device == AsrDevice.CPU:
        return ["CPUExecutionProvider"]
    if device == AsrDevice.CUDA:
        return [
            ("CUDAExecutionProvider", {"device_id": device_id}),
            "CPUExecutionProvider",
        ]
    # AUTO: 不在此处静态解析
    return []


def default_model_dir() -> Path:
    """
    返回仓库内 assets/models/sensevoice_onnx 目录。
    src/dictashift/asr/providers.py -> parents[3] 为仓库根。
    """
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "assets" / "models" / "sensevoice_onnx"


def model_paths(model_dir: Path, variant: str):
    """
    根据 variant 返回 (encoder_path, decoder_path, tokenizer_path)。
    variant 形如 "fp16" / "int8"。
    """
    encoder_path = model_dir / f"SenseVoice-Encoder.{variant}.onnx"
    decoder_path = model_dir / f"SenseVoice-CTC.{variant}.onnx"
    tokenizer_path = model_dir / "Tokenizer.bpe.model"
    return encoder_path, decoder_path, tokenizer_path
