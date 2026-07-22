"""
SenseVoice ONNX 数据类型定义

包含用于 SenseVoice 推理的数据类，提供类型安全和清晰的结构。
"""

from dataclasses import dataclass, field
from typing import List, Optional


# ==================== 识别结果相关 ====================

@dataclass
class RecognitionResult:
    """
    单个识别单元结果 (字符或 Token 块)

    Attributes:
        text: 识别文本 (字符或块)
        start: 起始时间（秒）
        is_hotword: 是否为命中的热词
    """
    text: str
    start: float
    is_hotword: bool = False


@dataclass
class Timings:
    """
    各阶段耗时统计（秒）

    Attributes:
        frontend: 特征提取耗时
        encoder: 编码器推理耗时
        decoder: 解码器 (CTC) 推理耗时
        radar: 热词雷达扫描耗时
        integrate: 结果整合耗时
        total: 总耗时
    """
    frontend: float = 0.0
    encoder: float = 0.0
    decoder: float = 0.0
    radar: float = 0.0
    integrate: float = 0.0
    total: float = 0.0


@dataclass
class TranscriptionResult:
    """
    完整的转录结果包装

    Attributes:
        text: 最终识别文本
        results: 详细的 RecognitionResult 列表
        hotwords: 识别到的热词列表
        timings: 耗时统计
    """
    text: str = ""
    results: List[RecognitionResult] = field(default_factory=list)
    hotwords: List[str] = field(default_factory=list)
    timings: Timings = field(default_factory=Timings)


# ==================== 引擎配置相关 ====================

@dataclass
class ASREngineConfig:
    """
    ASR 引擎配置参数

    Attributes:
        encoder_path: 编码器模型路径 (.onnx)
        decoder_path: 解码器模型路径 (.onnx)
        tokenizer_path: 分词器模型路径 (.model)
        providers: 推理后端 provider 列表 (ort 格式)
        hotwords: 初始热词字符串列表
        top_k: 热词搜索 Top-K 深度
        itn: 是否启用反向文本规范化
    """
    encoder_path: str
    decoder_path: str
    tokenizer_path: str
    providers: list = field(default_factory=lambda: ["CPUExecutionProvider"])
    hotwords: Optional[List[str]] = None
    top_k: int = 10
    itn: bool = True


# ==================== 导出列表 ====================

__all__ = [
    'RecognitionResult',
    'TranscriptionResult',
    'ASREngineConfig',
    'Timings',
]
