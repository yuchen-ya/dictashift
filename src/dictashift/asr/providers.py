import os
from enum import StrEnum
from pathlib import Path

# 模型目录下必须存在的三个文件名（providers 与 model_download 共用）。
# 就绪判断检查文件类型和非空状态。
REQUIRED_MODEL_FILES = (
    "SenseVoice-Encoder.int8.onnx",
    "SenseVoice-CTC.int8.onnx",
    "Tokenizer.bpe.model",
)


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


def user_model_dir() -> Path:
    """用户级模型目录：%APPDATA%\\DictaShift\\models\\sensevoice_onnx。

    无 APPDATA 环境变量时回退到 ~/.dictashift/models/sensevoice_onnx。
    不保证目录存在；调用方（下载器）负责创建。
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "DictaShift"
    else:
        base = Path.home() / ".dictashift"
    return base / "models" / "sensevoice_onnx"


def _repo_assets_model_dir() -> Path:
    """仓库内 assets/models/sensevoice_onnx 目录（仅开发版兜底）。

    src/dictashift/asr/providers.py -> parents[3] 为仓库根。
    """
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "assets" / "models" / "sensevoice_onnx"


def models_ready(model_dir: Path) -> bool:
    """判断 model_dir 下三个必需模型文件是否都就绪。

    就绪 = 存在、是普通文件（拒绝同名目录）且非空（拒绝 0 字节半成品）。
    只做廉价文件系统检查，不读内容/哈希；内容损坏由下载器校验与
    引擎加载失败提示兜底（设置窗口对已安装模型仍提供重新下载）。
    """
    for name in REQUIRED_MODEL_FILES:
        p = Path(model_dir) / name
        try:
            if not p.is_file() or p.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


def default_model_dir() -> Path:
    """模型目录解析顺序：
    1. user_model_dir() 下三个模型文件齐全 -> 用它（打包版/下载版）。
    2. 否则仓库 assets/models/sensevoice_onnx（开发版兜底）。

    检查文件类型及非空状态，内容由下载校验和引擎加载验证。
    """
    user_dir = user_model_dir()
    if models_ready(user_dir):
        return user_dir
    return _repo_assets_model_dir()


def model_paths(model_dir: Path):
    """
    返回 (encoder_path, decoder_path, tokenizer_path)。
    模型文件名固定 int8（第一版只支持 CPU 推理）。
    """
    encoder_path = model_dir / "SenseVoice-Encoder.int8.onnx"
    decoder_path = model_dir / "SenseVoice-CTC.int8.onnx"
    tokenizer_path = model_dir / "Tokenizer.bpe.model"
    return encoder_path, decoder_path, tokenizer_path
