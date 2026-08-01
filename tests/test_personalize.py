"""个性化输出后处理测试（PersonalizeSettings + PersonalizeProcessor）。

测试目标：
- PersonalizeSettings：序列化往返、缺失键回退默认、非法 rewrite_style 归一 off。
- PersonalizeProcessor.apply：
  * 顺序固定（改写 -> 后缀）；
  * 仅后缀 / 仅改写 / 全关；
  * LLM 异常 -> 回退原文且仍加后缀（绝不抛出）；
  * 空/全空白改写结果 -> 回退原文；
  * 活动档案缺失 / 非 openai_compatible -> 跳过改写（后缀不受影响）。
- build_personalize_processor：从 AppSettings 解析活动 LLM 档案。

LLM 一律用 fake backend mock，不发起真实网络。
"""

from __future__ import annotations

from dictashift.personalize import (
    SYSTEM_PROMPT_TOXIC,
    SYSTEM_PROMPT_TSUNDERE,
    PersonalizeProcessor,
    build_personalize_processor,
)
from dictashift.settings import AppSettings, PersonalizeSettings, TranslationSettings
from dictashift.translation import TranslationProfile


# ==================== Fakes ====================


class FakeRewriteBackend:
    """记录 rewrite_stream 调用、可控制返回内容与异常的 fake LLM 后端。"""

    def __init__(
        self,
        chunks: list[str] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._chunks = chunks if chunks is not None else ["改写", "结果"]
        self._raise = raise_on_call
        self.calls: list[tuple[str, TranslationProfile, str]] = []

    def rewrite_stream(self, text: str, profile: TranslationProfile, system_prompt: str):
        self.calls.append((text, profile, system_prompt))
        if self._raise is not None:
            raise self._raise
        yield from self._chunks


_PROFILE = TranslationProfile(
    name="myLLM", base_url="http://x/v1", model="m", api_key="k"
)


# ==================== PersonalizeSettings 模型 ====================


class TestPersonalizeSettingsModel:
    def test_defaults(self) -> None:
        p = PersonalizeSettings()
        assert p.suffix_enabled is False
        assert p.suffix_text == "喵"
        assert p.rewrite_style == "off"

    def test_round_trip(self) -> None:
        p = PersonalizeSettings(
            suffix_enabled=True, suffix_text="捏", rewrite_style="tsundere"
        )
        assert PersonalizeSettings.from_dict(p.to_dict()) == p

    def test_from_dict_missing_keys_uses_defaults(self) -> None:
        assert PersonalizeSettings.from_dict(None) == PersonalizeSettings()
        assert PersonalizeSettings.from_dict({}) == PersonalizeSettings()
        p = PersonalizeSettings.from_dict({"suffix_enabled": True})
        assert p.suffix_enabled is True
        assert p.suffix_text == "喵"
        assert p.rewrite_style == "off"

    def test_from_dict_invalid_rewrite_style_falls_back_off(self) -> None:
        p = PersonalizeSettings.from_dict({"rewrite_style": "weird"})
        assert p.rewrite_style == "off"

    def test_from_dict_valid_rewrite_styles_preserved(self) -> None:
        for style in ("off", "toxic", "tsundere"):
            assert PersonalizeSettings.from_dict({"rewrite_style": style}).rewrite_style == style

    def test_app_settings_round_trip_includes_personalize(self) -> None:
        s = AppSettings()
        s.personalize = PersonalizeSettings(
            suffix_enabled=True, suffix_text="哦", rewrite_style="toxic"
        )
        s2 = AppSettings.from_dict(s.to_dict())
        assert s2.personalize == s.personalize

    def test_app_settings_from_dict_missing_personalize_uses_defaults(self) -> None:
        s = AppSettings.from_dict({"enabled": True})
        assert s.personalize == PersonalizeSettings()


# ==================== PersonalizeProcessor.apply ====================


class TestProcessorApply:
    def test_all_off_returns_original(self) -> None:
        backend = FakeRewriteBackend()
        proc = PersonalizeProcessor(profile=_PROFILE, backend=backend)
        assert proc.apply("原文") == "原文"
        assert backend.calls == []
        assert proc.rewrite_active() is False

    def test_suffix_only(self) -> None:
        backend = FakeRewriteBackend()
        proc = PersonalizeProcessor(
            profile=_PROFILE, suffix_enabled=True, suffix_text="喵", backend=backend
        )
        assert proc.apply("原文") == "原文喵"
        # 不改写：不触网
        assert backend.calls == []

    def test_rewrite_only(self) -> None:
        backend = FakeRewriteBackend(chunks=["毒舌", "版"])
        proc = PersonalizeProcessor(
            profile=_PROFILE, rewrite_style="toxic", backend=backend
        )
        assert proc.rewrite_active() is True
        assert proc.apply("原文") == "毒舌版"
        # 流式增量被聚合；毒舌 prompt 被使用
        assert backend.calls[0][0] == "原文"
        assert backend.calls[0][2] == SYSTEM_PROMPT_TOXIC

    def test_rewrite_then_suffix_order(self) -> None:
        """顺序固定：先改写再后缀（后缀拼在改写结果之后）。"""
        backend = FakeRewriteBackend(chunks=["改写后"])
        proc = PersonalizeProcessor(
            profile=_PROFILE,
            suffix_enabled=True,
            suffix_text="喵",
            rewrite_style="tsundere",
            backend=backend,
        )
        assert proc.apply("原文") == "改写后喵"
        assert backend.calls[0][2] == SYSTEM_PROMPT_TSUNDERE

    def test_llm_exception_falls_back_and_still_appends_suffix(self) -> None:
        """LLM 任何异常 -> 原文继续 + 仍加后缀，绝不抛出。"""
        backend = FakeRewriteBackend(raise_on_call=RuntimeError("boom"))
        proc = PersonalizeProcessor(
            profile=_PROFILE,
            suffix_enabled=True,
            suffix_text="喵",
            rewrite_style="toxic",
            backend=backend,
        )
        assert proc.apply("原文") == "原文喵"

    def test_llm_exception_without_suffix_returns_original(self) -> None:
        backend = FakeRewriteBackend(raise_on_call=RuntimeError("boom"))
        proc = PersonalizeProcessor(
            profile=_PROFILE, rewrite_style="toxic", backend=backend
        )
        assert proc.apply("原文") == "原文"

    def test_empty_rewrite_result_falls_back(self) -> None:
        """空/全空白改写结果 -> 回退原文。"""
        for chunks in ([], ["", ""], ["   ", "\n"]):
            backend = FakeRewriteBackend(chunks=chunks)
            proc = PersonalizeProcessor(
                profile=_PROFILE, rewrite_style="toxic", backend=backend
            )
            assert proc.apply("原文") == "原文"

    def test_no_profile_skips_rewrite_but_keeps_suffix(self) -> None:
        """活动档案缺失 -> 跳过改写（不触网），后缀不受影响。"""
        backend = FakeRewriteBackend()
        proc = PersonalizeProcessor(
            profile=None,
            suffix_enabled=True,
            suffix_text="喵",
            rewrite_style="toxic",
            backend=backend,
        )
        assert proc.rewrite_active() is False
        # 无句末标点 -> 末尾追加
        assert proc.apply("原文") == "原文喵"
        assert backend.calls == []

    def test_empty_suffix_text_not_appended(self) -> None:
        proc = PersonalizeProcessor(suffix_enabled=True, suffix_text="")
        assert proc.apply("原文") == "原文"


# ==================== 句尾后缀：逐句插入 ====================


class TestSuffixPerSentence:
    """逐句插入：在句末标点之前插入 suffix；无标点末尾追加。

    测试直接用 PersonalizeProcessor(suffix_enabled=True)，后缀逻辑不走 LLM。
    """

    def _proc(self, suffix: str) -> PersonalizeProcessor:
        return PersonalizeProcessor(suffix_enabled=True, suffix_text=suffix)

    def test_multi_chinese_sentences(self) -> None:
        # 用户实际反馈场景
        assert self._proc("喵").apply("这是一次对话测试。你好。") == "这是一次对话测试喵。你好喵。"

    def test_exclamation_and_question_marks(self) -> None:
        # ！？!? 均触发
        assert self._proc("喵").apply("真的吗？太好了！") == "真的吗喵？太好了喵！"
        assert self._proc("nya").apply("What? Yes!") == "Whatnya? Yesnya!"

    def test_english_period(self) -> None:
        assert self._proc("nya").apply("Hello world.") == "Hello worldnya."

    def test_no_sentence_end_appends_at_tail(self) -> None:
        assert self._proc("喵").apply("你好") == "你好喵"
        assert self._proc("nya").apply("Hi there") == "Hi therenya"

    def test_comma_not_treated_as_sentence_end(self) -> None:
        # 逗号、顿号是句中标点，不触发；无句末标点 -> 末尾追加
        assert self._proc("喵").apply("你好，世界") == "你好，世界喵"
        assert self._proc("喵").apply("测试、顿号") == "测试、顿号喵"

    def test_mixed_cn_en_punctuation(self) -> None:
        # 中英文标点混合
        assert (
            self._proc("喵").apply("你好。World! 结束。")
            == "你好喵。World喵! 结束喵。"
        )

    def test_ellipsis_treated_as_whole(self) -> None:
        # 省略号 ... 不拆成三个句末；末尾因无其他句末标点 -> 末尾追加
        # 行为锁定：... 中的点不触发，整段视为无句末标点 -> 末尾追加。
        assert self._proc("喵").apply("What...") == "What...喵"

    def test_empty_or_whitespace_no_suffix(self) -> None:
        assert self._proc("喵").apply("") == ""
        assert self._proc("喵").apply("   ") == "   "
        assert self._proc("喵").apply("\n\t") == "\n\t"

    def test_empty_suffix_no_change(self) -> None:
        assert self._proc("").apply("你好。") == "你好。"

    def test_multiple_english_sentences(self) -> None:
        assert self._proc(".").apply("a. b. c.") == "a.. b.. c.."

    def test_decimal_dot_not_treated_as_sentence_end(self) -> None:
        # 小数点（. 后跟数字）不触发插入；中文句号正常生效。
        assert self._proc("喵").apply("价格是3.14。") == "价格是3.14喵。"
        # 纯小数无句末标点 -> 末尾追加。
        assert self._proc("喵").apply("3.14") == "3.14喵"
        # 句中含小数点：句点正常插入，小数点跳过。
        assert (
            self._proc("nya").apply("I like it. 3.5 is fine.")
            == "I like itnya. 3.5 is finenya."
        )
        # 中文混合小数 + 句末标点。
        assert self._proc("喵").apply("价格9.9元。") == "价格9.9元喵。"

    def test_suffix_after_rewrite_uses_per_sentence(self) -> None:
        """改写后再逐句加后缀：改写结果含句末标点时按句插入。"""
        backend = FakeRewriteBackend(chunks=["第一句。第二句。"])
        proc = PersonalizeProcessor(
            profile=_PROFILE,
            suffix_enabled=True,
            suffix_text="喵",
            rewrite_style="toxic",
            backend=backend,
        )
        assert proc.apply("原文") == "第一句喵。第二句喵。"

    def test_rewrite_exception_falls_back_and_per_sentence_suffix(self) -> None:
        """改写失败回退原文时，逐句加后缀逻辑照常生效。"""
        backend = FakeRewriteBackend(raise_on_call=RuntimeError("boom"))
        proc = PersonalizeProcessor(
            profile=_PROFILE,
            suffix_enabled=True,
            suffix_text="喵",
            rewrite_style="toxic",
            backend=backend,
        )
        assert proc.apply("第一句。第二句。") == "第一句喵。第二句喵。"


# ==================== build_personalize_processor ====================


def _settings_with_active(
    active: str, profiles: list[TranslationProfile]
) -> AppSettings:
    s = AppSettings()
    s.translation = TranslationSettings(active_profile=active, profiles=profiles)
    return s


class TestBuildProcessor:
    def test_resolves_openai_compatible_active_profile(self) -> None:
        s = _settings_with_active("myLLM", [_PROFILE])
        s.personalize = PersonalizeSettings(
            suffix_enabled=True, suffix_text="捏", rewrite_style="toxic"
        )
        backend = FakeRewriteBackend(chunks=["x"])
        proc = build_personalize_processor(s, backend=backend)
        assert proc.rewrite_active() is True
        assert proc.apply("原文") == "x捏"
        # 复用翻译活动档案（base_url/model/api_key 来自该 profile）
        assert backend.calls[0][1] == _PROFILE

    def test_builtin_free_active_profile_skips_rewrite(self) -> None:
        """活动档案为内置免登录项（不在用户 profiles 中）-> 改写跳过。"""
        from dictashift.translation import BUILTIN_GOOGLE

        s = _settings_with_active(BUILTIN_GOOGLE.name, [])
        s.personalize = PersonalizeSettings(rewrite_style="toxic", suffix_enabled=True)
        backend = FakeRewriteBackend()
        proc = build_personalize_processor(s, backend=backend)
        assert proc.rewrite_active() is False
        assert proc.apply("原文") == "原文喵"
        assert backend.calls == []

    def test_non_openai_compatible_profile_skips_rewrite(self) -> None:
        """活动档案非 openai_compatible 型 -> 改写跳过。"""
        free = TranslationProfile(name="g", base_url="", model="", backend_type="google")
        s = _settings_with_active("g", [free])
        s.personalize = PersonalizeSettings(rewrite_style="toxic")
        backend = FakeRewriteBackend()
        proc = build_personalize_processor(s, backend=backend)
        assert proc.rewrite_active() is False
        assert proc.apply("原文") == "原文"
        assert backend.calls == []

    def test_no_active_profile_skips_rewrite(self) -> None:
        s = _settings_with_active("", [_PROFILE])
        s.personalize = PersonalizeSettings(rewrite_style="toxic")
        proc = build_personalize_processor(s, backend=FakeRewriteBackend())
        assert proc.rewrite_active() is False
