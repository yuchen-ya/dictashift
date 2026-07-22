"""DictaShift 翻译模块：OpenAI-compatible 流式 + 免费服务（Google/Bing）。"""

from .base import TranslationProfile, TranslationBackend, TranslationError
from .profiles import build_translate_en_profile, SYSTEM_PROMPT_ZH2EN
from .openai_compatible import OpenAICompatibleBackend
from .llama_server import LlamaServerBackend, DEFAULT_LLAMA_PORT
from .google_free import GoogleFreeBackend
from .bing_edge import BingEdgeBackend
from .failover import (
    FreeFailoverBackend,
    DispatcherBackend,
    RATE_LIMIT_STATUS,
)

__all__ = [
    "TranslationProfile",
    "TranslationBackend",
    "TranslationError",
    "OpenAICompatibleBackend",
    "LlamaServerBackend",
    "GoogleFreeBackend",
    "BingEdgeBackend",
    "FreeFailoverBackend",
    "DispatcherBackend",
    "RATE_LIMIT_STATUS",
    "build_translate_en_profile",
    "SYSTEM_PROMPT_ZH2EN",
    "DEFAULT_LLAMA_PORT",
]
