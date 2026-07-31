"""个性化输出后处理器：语气改写（LLM 流式聚合）+ 句尾后缀（纯字符串拼接）。

管线位置：ASR -> 翻译（如启用） -> 本处理器 -> 粘贴输出。
本处理器内部顺序固定：先语气改写，再追加句尾后缀；两者独立开关、可叠加。

失败静默回退（不变量）：
- 活动档案缺失 / 档案非 openai_compatible 型 / LLM 调用任何异常 -> 记日志、
  用原文继续，绝不抛出、绝不阻断输出。
- 改写失败不影响后缀追加（改写失败也要加后缀）。
- 改写结果为空/全空白 -> 回退原文。

线程模型：apply 在 controller worker 线程同步调用（LLM 流式聚合为阻塞 I/O），
绝不在 UI 主线程调用。
"""

from __future__ import annotations

import logging

from ..settings import AppSettings
from ..translation import OpenAICompatibleBackend, TranslationProfile

logger = logging.getLogger(__name__)


# 毒舌：犀利吐槽但保留原意；只输出改写后文本、保持原语言。
SYSTEM_PROMPT_TOXIC = (
    "你是语气改写器。把用户输入的文本改写为毒舌口吻：犀利、带刺、爱吐槽，"
    "但必须完整保留原文的意思与信息，不得新增或删减事实。"
    "只输出改写后的文本，不要解释、不要输出原文，保持原文的语言。"
)

# 傲娇：tsundere 口吻；只输出改写后文本、保持原语言。
SYSTEM_PROMPT_TSUNDERE = (
    "你是语气改写器。把用户输入的文本改写为傲娇（tsundere）口吻：口是心非、"
    "嘴硬心软，但必须完整保留原文的意思与信息，不得新增或删减事实。"
    "只输出改写后的文本，不要解释、不要输出原文，保持原文的语言。"
)

# rewrite_style -> system prompt 映射（"off" 不在其中，表示不改写）。
_STYLE_PROMPTS = {
    "toxic": SYSTEM_PROMPT_TOXIC,
    "tsundere": SYSTEM_PROMPT_TSUNDERE,
}


class PersonalizeProcessor:
    """个性化输出后处理器。

    apply(text) -> str：
    1. rewrite_style != "off" 且有可用 LLM 档案时，调 LLM 流式聚合改写；
       任何异常或空结果 -> 回退原文。
    2. suffix_enabled 且 suffix_text 非空时，在（可能改写后的）文本末尾拼接后缀。
    """

    def __init__(
        self,
        *,
        profile: TranslationProfile | None = None,
        suffix_enabled: bool = False,
        suffix_text: str = "喵",
        rewrite_style: str = "off",
        backend: OpenAICompatibleBackend | None = None,
    ) -> None:
        # profile：LLM 改写用的活动档案（复用翻译页配置）；None 表示不可用。
        self._profile = profile
        self._suffix_enabled = bool(suffix_enabled)
        self._suffix_text = suffix_text
        self._rewrite_style = rewrite_style
        self._backend = backend if backend is not None else OpenAICompatibleBackend()

    def rewrite_active(self) -> bool:
        """当前配置下 apply 是否会实际执行 LLM 改写（供管线决定是否进入改写状态）。"""
        return self._rewrite_style in _STYLE_PROMPTS and self._profile is not None

    def apply(self, text: str) -> str:
        """应用后处理：先改写再后缀；任何失败静默回退，绝不抛出。"""
        result = text
        if self.rewrite_active():
            result = self._rewrite_or_original(result)
        if self._suffix_enabled and self._suffix_text:
            result = result + self._suffix_text
        return result

    def _rewrite_or_original(self, text: str) -> str:
        """调 LLM 改写；异常或空结果回退原文。"""
        prompt = _STYLE_PROMPTS[self._rewrite_style]
        try:
            rewritten = "".join(
                self._backend.rewrite_stream(text, self._profile, prompt)
            )
        except Exception:  # noqa: BLE001 - 失败静默回退，绝不阻断输出
            logger.warning(
                "[PersonalizeProcessor] LLM 改写失败，回退原文", exc_info=True
            )
            return text
        if not rewritten.strip():
            logger.warning("[PersonalizeProcessor] LLM 改写结果为空，回退原文")
            return text
        return rewritten


def build_personalize_processor(
    settings: AppSettings,
    backend: OpenAICompatibleBackend | None = None,
) -> PersonalizeProcessor:
    """从 AppSettings 构建 PersonalizeProcessor（跟随「应用/确定」的设置生效节奏）。

    LLM 改写复用翻译页的活动档案：active_profile 命中的用户档案且
    backend_type == "openai_compatible" 时才可用；否则 profile=None
    （改写自动跳过，后缀不受影响）。
    """
    p = settings.personalize
    return PersonalizeProcessor(
        profile=_resolve_active_llm_profile(settings),
        suffix_enabled=p.suffix_enabled,
        suffix_text=p.suffix_text,
        rewrite_style=p.rewrite_style,
        backend=backend,
    )


def _resolve_active_llm_profile(settings: AppSettings) -> TranslationProfile | None:
    """解析 LLM 改写可用的活动档案；不可用（缺失/非 openai_compatible）返回 None。"""
    active = settings.translation.active_profile
    if not active:
        return None
    for profile in settings.translation.profiles:
        if profile.name == active and profile.backend_type == "openai_compatible":
            return profile
    return None
