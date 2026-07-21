"""DictaShift 翻译模块：OpenAI-compatible 流式翻译客户端。"""

from .base import TranslationProfile, TranslationBackend, TranslationError
from .profiles import build_translate_en_profile, SYSTEM_PROMPT_ZH2EN
from .openai_compatible import OpenAICompatibleBackend
from .llama_server import LlamaServerBackend, DEFAULT_LLAMA_PORT

__all__ = [
    "TranslationProfile",
    "TranslationBackend",
    "TranslationError",
    "OpenAICompatibleBackend",
    "LlamaServerBackend",
    "build_translate_en_profile",
    "SYSTEM_PROMPT_ZH2EN",
    "DEFAULT_LLAMA_PORT",
]
