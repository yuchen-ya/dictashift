"""翻译后端抽象接口与数据类型。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class TranslationProfile:
    """翻译配置档案（不可变，作为任务快照直接使用）。

    不变量：每个语音任务在创建时冻结一份 profile 副本，
    用户后续切换模型/后端只影响之后新建的任务。
    """

    name: str
    base_url: str          # 例如 http://127.0.0.1:19120/v1 或云端地址
    model: str
    api_key: str | None = None
    temperature: float = 0.3
    timeout: float = 60.0


class TranslationError(Exception):
    """翻译后端异常。

    不变量：日志/异常信息中不得包含 api_key 或 Authorization 头内容。
    status_code 为 None 表示网络/超时等非 HTTP 状态错误。
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TranslationBackend(ABC):
    """翻译后端抽象基类。"""

    @abstractmethod
    def translate_stream(self, text: str, profile: TranslationProfile) -> Iterator[str]:
        """流式翻译，逐段 yield 增量文本（delta）。

        同步生成器，由调用方放后台线程消费。
        """

    def translate(self, text: str, profile: TranslationProfile) -> str:
        """便捷方法：消费全部流式增量拼接为完整结果。默认实现调用 translate_stream。"""
        return "".join(self.translate_stream(text, profile))
