"""DictaShift 翻译模块：OpenAI-compatible 流式 + 免费服务（Google/Bing）。"""

from .base import TranslationProfile, TranslationBackend, TranslationError
from .profiles import (
    BUILTIN_BING,
    BUILTIN_GOOGLE,
    SYSTEM_PROMPT_ZH2EN,
    build_translate_en_profile,
    builtin_free_profiles,
    builtin_name_for_backend_type,
    builtin_profile_by_name,
    builtin_profile_names,
)
from .openai_compatible import (
    OpenAICompatibleBackend,
    build_no_think_candidates,
    fetch_available_models,
)
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
    "build_no_think_candidates",
    "fetch_available_models",
    "LlamaServerBackend",
    "GoogleFreeBackend",
    "BingEdgeBackend",
    "FreeFailoverBackend",
    "DispatcherBackend",
    "RATE_LIMIT_STATUS",
    "build_translate_en_profile",
    "SYSTEM_PROMPT_ZH2EN",
    "DEFAULT_LLAMA_PORT",
    "BUILTIN_GOOGLE",
    "BUILTIN_BING",
    "builtin_free_profiles",
    "builtin_profile_names",
    "builtin_profile_by_name",
    "builtin_name_for_backend_type",
]
