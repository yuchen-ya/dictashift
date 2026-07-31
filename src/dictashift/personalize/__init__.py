"""DictaShift 个性化输出后处理：语气改写（LLM）+ 句尾后缀（纯拼接）。"""

from .processor import (
    SYSTEM_PROMPT_TOXIC,
    SYSTEM_PROMPT_TSUNDERE,
    PersonalizeProcessor,
    build_personalize_processor,
)

__all__ = [
    "SYSTEM_PROMPT_TOXIC",
    "SYSTEM_PROMPT_TSUNDERE",
    "PersonalizeProcessor",
    "build_personalize_processor",
]
