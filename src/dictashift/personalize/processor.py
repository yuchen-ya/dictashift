"""个性化输出后处理器：语气改写（LLM 流式聚合）+ 句尾后缀（纯字符串拼接）。

管线位置：ASR -> 翻译（如启用） -> 本处理器 -> 粘贴输出。
本处理器内部顺序固定：先语气改写，再追加句尾后缀；两者独立开关、可叠加。

句尾后缀语义（逐句插入）：
- 按句末标点（。！？!? 与英文 .）切分文本，在**每一个句子的句末标点之前**
  插入 suffix（如 "测试。你好。" -> "测试喵。你好喵。"）。
- 英文省略号 "..." 视为整体，不拆成三个句点；小数/缩写中的 . 同理不作为切分点
  ——实现采用「...」或连续多个 . 视为整体，孤立单个 . 才作句末标点。
- 文本无任何句末标点 -> 末尾直接加 suffix（"你好" -> "你好喵"）。
- 空文本/全空白 -> 不加。
- 后缀纯字符串拼接，绝不走大模型。

失败静默回退（不变量）：
- 活动档案缺失 / 档案非 openai_compatible 型 / LLM 调用任何异常 -> 记日志、
  用原文继续，绝不抛出、绝不阻断输出。
- 改写失败不影响后缀追加（改写失败也要加后缀），逐句插入逻辑照常生效。

线程模型：apply 在 controller worker 线程同步调用（LLM 流式聚合为阻塞 I/O），
绝不在 UI 主线程调用。
"""

from __future__ import annotations

import logging
import re

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


# 句末标点匹配：。！？!?；以及英文单个 .（... 由下方正则先整体跳过）。
# 命中即在该标点之前插入 suffix；末尾若无句末标点则整体作为一段末尾追加。
_SENTENCE_END_RE = re.compile(r"[。！？!?]")


def _insert_suffix_per_sentence(text: str, suffix: str) -> str:
    """在文本的每个句末标点之前插入 suffix；无句末标点时末尾追加。

    规则要点：
    - 句末标点：。！？!? 与英文句点 .（孤立单个）。
    - 英文省略号 "..."（连续 >=2 个 .）视为整体不拆分：先把连续句点替换为
      占位符、在剩余单点上做插入、再把占位符还原为原始的连续句点。
    - 整段文本无任何句末标点 -> 直接在末尾追加 suffix。
    - 空文本/全空白 -> 原样返回。
    """
    if not text or not text.strip() or not suffix:
        return text

    # 1) 临时用占位符保护连续的英文句点（省略号 ...），避免被当作三个单点。
    _DOTS_PLACEHOLDER = "\x00DOTS\x01"
    protected = re.sub(r"\.{2,}", _DOTS_PLACEHOLDER, text)

    # 2) 在每个句末标点（中文标点 或 英文单 .）之前插入 suffix。
    def _insert_before_punct(m: re.Match) -> str:
        return suffix + m.group(0)

    # 中文句末标点直接插。
    inserted = _SENTENCE_END_RE.sub(_insert_before_punct, protected)
    # 英文单 .：保护占位符之外的单个 .（... 已被占位符取代，不会被命中）。
    inserted = re.sub(r"\.", _insert_before_punct, inserted)

    # 3) 还原占位符为原始的省略号。
    inserted = inserted.replace(_DOTS_PLACEHOLDER, "...")

    # 4) 若原文本完全没有任何句末标点 -> 末尾追加 suffix。
    if not _has_sentence_end(text):
        inserted = inserted + suffix
    return inserted


def _has_sentence_end(text: str) -> bool:
    """文本是否含至少一个句末标点（中文标点 或 英文孤立单点）。

    连续的英文句点（省略号 ...）整体不算句末标点：判定时先把 ... 屏蔽，
    再看是否还有孤立的单点或中文句末标点。
    """
    if any(ch in text for ch in "。！？!?"):
        return True
    # 屏蔽连续句点（...）后看是否还有单点。
    masked = re.sub(r"\.{2,}", "", text)
    return "." in masked


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
            result = _insert_suffix_per_sentence(result, self._suffix_text)
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
