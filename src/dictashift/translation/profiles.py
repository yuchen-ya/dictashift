"""翻译配置档案构造与系统提示词常量。

不负责 profiles 的持久化（那是 settings 模块的职责），
这里只提供构造与常量。
"""

from .base import TranslationProfile


# 中译英系统提示词：只输出译文，不要解释、不要输出原文。
SYSTEM_PROMPT_ZH2EN = (
    "将用户输入的中文翻译为英文，只输出译文，不要解释、不要输出原文。"
)


# ==================== 内置免登录翻译服务 ====================


# 内置免登录翻译项：与用户的 LLM 配置平级出现在「当前翻译配置」下拉中，
# 不进入持久化的 profiles 列表（由 settings.from_dict 过滤掉非 openai_compatible 项）。
# backend_type 决定 DispatcherBackend 路由到对应免费后端。
BUILTIN_GOOGLE = TranslationProfile(
    name="Google 翻译（免费）",
    base_url="",
    model="",
    backend_type="google",
)
BUILTIN_BING = TranslationProfile(
    name="Bing 翻译（免费）",
    base_url="",
    model="",
    backend_type="bing",
)


def builtin_free_profiles() -> tuple[TranslationProfile, TranslationProfile]:
    """返回内置免登录翻译项 (Google, Bing)。"""
    return (BUILTIN_GOOGLE, BUILTIN_BING)


def builtin_profile_names() -> frozenset[str]:
    """返回内置免登录翻译项的 name 集合。"""
    return frozenset(p.name for p in builtin_free_profiles())


def builtin_profile_by_name(name: str) -> TranslationProfile | None:
    """按 name 查找内置免登录翻译项；未命中返回 None。"""
    for p in builtin_free_profiles():
        if p.name == name:
            return p
    return None


# backend_type -> 内置项 name 的映射，用于 settings 迁移时把旧版本保存的
# 免费 profile 的 active_profile 重映射到对应内置项。
def builtin_name_for_backend_type(backend_type: str) -> str | None:
    """把免费 backend_type 映射到对应内置项 name；非免费类型返回 None。"""
    mapping = {
        "google": BUILTIN_GOOGLE.name,
        "bing": BUILTIN_BING.name,
    }
    return mapping.get(backend_type)


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
