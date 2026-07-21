"""翻译配置档案构造与系统提示词常量。

不负责 profiles 的持久化（那是 settings 模块的职责），
这里只提供构造与常量。
"""

from .base import TranslationProfile


# 中译英系统提示词：只输出译文，不要解释、不要输出原文。
SYSTEM_PROMPT_ZH2EN = (
    "将用户输入的中文翻译为英文，只输出译文，不要解释、不要输出原文。"
)


def build_translate_en_profile(
    base_url: str,
    model: str,
    api_key: str | None = None,
    name: str = "中译英",
) -> TranslationProfile:
    """构造中译英档案（temperature=0.3）。"""
    return TranslationProfile(
        name=name,
        base_url=base_url,
        model=model,
        api_key=api_key,
        temperature=0.3,
    )
