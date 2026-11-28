"""应用配置数据模型。

每个 dataclass 提供 to_dict / from_dict；from_dict 对缺失键使用默认值
（无配置时的回退），忽略未知键。

不变量 (AGENTS.md 12)：
- 新增配置项必须说明默认值、迁移行为和无配置时的回退。
- 所有配置读写经由 SettingsRepository，业务模块不得自行散落读写。
- TranslationProfile 复用 translation.base 的 frozen dataclass，此处只做
  序列化适配，不重新定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..translation import (
    TranslationProfile,
    builtin_name_for_backend_type,
    builtin_profile_names,
)


# ==================== 翻译档案序列化 ====================


def profile_to_dict(profile: TranslationProfile) -> dict:
    """把 TranslationProfile (frozen dataclass) 序列化为 dict。

    单独抽出便于 settings 窗口与 repository 共用。
    temperature 为 None 时省略，保持配置干净；非 None 时保留以兼容旧数据往返。
    """
    data: dict = {
        "name": profile.name,
        "base_url": profile.base_url,
        "model": profile.model,
        "api_key": profile.api_key,
        "timeout": profile.timeout,
        "backend_type": profile.backend_type,
        "direction": profile.direction,
    }
    if profile.temperature is not None:
        data["temperature"] = profile.temperature
    return data


def profile_from_dict(d: dict) -> TranslationProfile:
    """从 dict 构造 TranslationProfile，缺失键使用 TranslationProfile 默认值。"""
    raw_temp = d.get("temperature")
    temperature = (
        float(raw_temp)
        if isinstance(raw_temp, (int, float)) and not isinstance(raw_temp, bool)
        else None
    )
    return TranslationProfile(
        name=d.get("name", ""),
        base_url=d.get("base_url", ""),
        model=d.get("model", ""),
        api_key=d.get("api_key"),
        temperature=temperature,
        timeout=d.get("timeout", 60.0),
        # 缺失时默认 openai_compatible（旧配置无感迁移）
        backend_type=d.get("backend_type", "openai_compatible"),
        # 缺失时默认 zh2en（旧配置无感迁移）；未知值不在此归一，
        # 由后端防御性按 zh2en 处理（见 translation/base.py 字段注释）
        direction=d.get("direction", "zh2en"),
    )


def default_cloud_profile() -> TranslationProfile:
    """返回一个占位云端档案，供设置窗口初始化用。

    不含 api_key（None）；用户需自行填入。
    """
    return TranslationProfile(
        name="云端 OpenAI",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key=None,
        timeout=60.0,
    )


# ==================== 子配置 ====================


@dataclass
class HotkeySettings:
    """快捷键配置（归一化前的配置字符串）。

    默认值：dictate_zh=f8, dictate_en=f9, toggle_enabled=ctrl+alt+f12, cancel=esc,
    selection_translate=f7（划词翻译开关；旧配置缺该键时回退 f7）,
    quick_lookup=alt+q（快速查词；旧配置缺该键时回退 alt+q）。
    """

    dictate_zh: str = "f8"
    dictate_en: str = "f9"
    toggle_enabled: str = "ctrl+alt+f12"
    cancel: str = "esc"
    selection_translate: str = "f7"
    quick_lookup: str = "alt+q"

    def to_dict(self) -> dict:
        return {
            "dictate_zh": self.dictate_zh,
            "dictate_en": self.dictate_en,
            "toggle_enabled": self.toggle_enabled,
            "cancel": self.cancel,
            "selection_translate": self.selection_translate,
            "quick_lookup": self.quick_lookup,
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "HotkeySettings":
        if not d:
            return cls()
        return cls(
            dictate_zh=d.get("dictate_zh", "f8"),
            dictate_en=d.get("dictate_en", "f9"),
            toggle_enabled=d.get("toggle_enabled", "ctrl+alt+f12"),
            cancel=d.get("cancel", "esc"),
            selection_translate=d.get("selection_translate", "f7"),
            quick_lookup=d.get("quick_lookup", "alt+q"),
        )


@dataclass
class AsrSettings:
    """ASR 配置。

    device/device_id 字段已随 GPU 路线移除（推理设备固定为 AUTO/CPU，等价）。
    from_dict 本就忽略未知键，旧配置文件中的 device/device_id 键自然失效，
    无需额外迁移代码。

    默认值：max_record_seconds=60, mic_device_index=None（表示使用系统默认输入设备）。
    """

    max_record_seconds: int = 60
    mic_device_index: int | None = None
    instant_recording: bool = False

    def to_dict(self) -> dict:
        return {
            "max_record_seconds": self.max_record_seconds,
            "mic_device_index": self.mic_device_index,
            "instant_recording": self.instant_recording,
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "AsrSettings":
        if not d:
            return cls()
        return cls(
            max_record_seconds=d.get("max_record_seconds", 60),
            mic_device_index=d.get("mic_device_index", None),
            instant_recording=d.get("instant_recording") is True,
        )


@dataclass
class TranslationSettings:
    """翻译配置：当前活动档案名 + 档案列表。

    默认 profiles 为空列表。active_profile 为空字符串表示无活动档案。
    """

    active_profile: str = ""
    profiles: list[TranslationProfile] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "active_profile": self.active_profile,
            "profiles": [profile_to_dict(p) for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "TranslationSettings":
        if not d:
            return cls()
        raw_profiles = d.get("profiles", []) or []
        # 过滤掉非 openai_compatible 的 profile（旧版本保存的免费 profile 由
        # 内置项 BUILTIN_GOOGLE/BUILTIN_BING 取代，不再作为用户配置持久化）。
        # 同时记录被过滤项的 backend_type，用于 active_profile 重映射。
        profiles: list[TranslationProfile] = []
        dropped_by_name: dict[str, TranslationProfile] = {}
        if isinstance(raw_profiles, list):
            for item in raw_profiles:
                if not isinstance(item, dict):
                    continue
                p = profile_from_dict(item)
                if p.backend_type == "openai_compatible":
                    profiles.append(p)
                else:
                    dropped_by_name[p.name] = p
        active_profile = d.get("active_profile", "")
        # active_profile 指向被过滤掉的免费 profile：按 backend_type 映射到内置项 name。
        if active_profile in dropped_by_name:
            mapped = builtin_name_for_backend_type(
                dropped_by_name[active_profile].backend_type
            )
            active_profile = mapped if mapped is not None else ""
        # active_profile 指向不存在的 name（既非保留项也非内置项）：置为 ""。
        # 注意：内置项 name 合法（不持久化在 profiles 中），不应被清除。
        elif (
            active_profile
            and active_profile not in builtin_profile_names()
            and not any(p.name == active_profile for p in profiles)
        ):
            active_profile = ""
        return cls(
            active_profile=active_profile,
            profiles=profiles,
        )


@dataclass
class UiSettings:
    """UI 配置。

    result_ui：直输失败时的结果展示方式。
    - "popup"（默认）：平时无悬浮窗；失败时在屏幕右下角弹通知式卡片（ResultPopup）。
    - "overlay"：常驻状态悬浮窗置顶显示状态；失败时转译内容+复制按钮直接显示在
      悬浮窗里（不弹 ResultPopup）。

    迁移（from_dict）：
    - 读到合法 result_ui（"popup"/"overlay"）直接使用。
    - 读不到 result_ui 时看旧键 show_overlay：True -> "overlay"，False/缺失 -> "popup"。
    - result_ui 值非法（非两者）-> 归一为 "popup"。
    to_dict 只写 result_ui（不再写 show_overlay）。

    launch_at_login 默认 False。
    """

    result_ui: str = "popup"
    launch_at_login: bool = False

    def to_dict(self) -> dict:
        return {
            "result_ui": self.result_ui,
            "launch_at_login": self.launch_at_login,
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "UiSettings":
        if not d:
            return cls()
        result_ui = d.get("result_ui")
        if result_ui in ("popup", "overlay"):
            pass
        elif result_ui is None:
            # 旧键迁移：show_overlay=True -> overlay；否则 popup。
            result_ui = "overlay" if d.get("show_overlay") is True else "popup"
        else:
            # 非法值归一为 popup。
            result_ui = "popup"
        return cls(
            result_ui=result_ui,
            launch_at_login=d.get("launch_at_login", False),
        )


# ==================== 个性化（输出后处理） ====================

# rewrite_style 合法值：off=不改写；toxic=毒舌；tsundere=傲娇。
REWRITE_STYLES = ("off", "toxic", "tsundere")


@dataclass
class PersonalizeSettings:
    """个性化输出后处理配置。

    管线顺序：翻译（如启用）-> 语气改写（rewrite_style != off 时走 LLM）
    -> 句尾后缀（纯字符串拼接，不走大模型）。三个环节独立开关、可叠加。

    默认值：suffix_enabled=False, suffix_text="喵", rewrite_style="off"
    （全部关闭，行为与旧版本一致）。

    迁移（from_dict）：缺失键用默认值；rewrite_style 非法值归一为 "off"
    （与 UiSettings.result_ui 的校验模式一致）。
    """

    suffix_enabled: bool = False
    suffix_text: str = "喵"
    rewrite_style: str = "off"

    def to_dict(self) -> dict:
        return {
            "suffix_enabled": self.suffix_enabled,
            "suffix_text": self.suffix_text,
            "rewrite_style": self.rewrite_style,
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "PersonalizeSettings":
        if not d:
            return cls()
        rewrite_style = d.get("rewrite_style", "off")
        if rewrite_style not in REWRITE_STYLES:
            # 非法值归一为 off。
            rewrite_style = "off"
        return cls(
            suffix_enabled=d.get("suffix_enabled", False),
            suffix_text=d.get("suffix_text", "喵"),
            rewrite_style=rewrite_style,
        )


# ==================== 划词翻译 ====================


@dataclass
class SelectionTranslateSettings:
    """划词翻译配置。

    enabled：总开关，默认 False（功能默认关闭，需用户显式开启）。
    auto_trigger：划选文字后自动弹出翻译，默认 True；关闭时鼠标划选不触发。
    popup_timeout_s：悬浮卡片自动关闭秒数，默认 10（设置页可调 5~60）。

    迁移（from_dict）：旧 settings.json 无 selection_translate 键时整体取默认值；
    字段缺失逐项回退默认；忽略未知键。
    """

    enabled: bool = False
    auto_trigger: bool = True
    popup_timeout_s: int = 10

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "auto_trigger": self.auto_trigger,
            "popup_timeout_s": self.popup_timeout_s,
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "SelectionTranslateSettings":
        if not d:
            return cls()
        return cls(
            enabled=d.get("enabled", False),
            auto_trigger=d.get("auto_trigger", True),
            popup_timeout_s=d.get("popup_timeout_s", 10),
        )


# ==================== 顶层配置 ====================


@dataclass
class AppSettings:
    """应用顶层配置：enabled + 六个子配置。

    默认 enabled=True。
    """

    enabled: bool = True
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
    asr: AsrSettings = field(default_factory=AsrSettings)
    translation: TranslationSettings = field(default_factory=TranslationSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    personalize: PersonalizeSettings = field(default_factory=PersonalizeSettings)
    selection_translate: SelectionTranslateSettings = field(
        default_factory=SelectionTranslateSettings
    )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "hotkeys": self.hotkeys.to_dict(),
            "asr": self.asr.to_dict(),
            "translation": self.translation.to_dict(),
            "ui": self.ui.to_dict(),
            "personalize": self.personalize.to_dict(),
            "selection_translate": self.selection_translate.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict | None = None) -> "AppSettings":
        if not d:
            return cls()
        return cls(
            enabled=d.get("enabled", True),
            hotkeys=HotkeySettings.from_dict(d.get("hotkeys")),
            asr=AsrSettings.from_dict(d.get("asr")),
            translation=TranslationSettings.from_dict(d.get("translation")),
            ui=UiSettings.from_dict(d.get("ui")),
            personalize=PersonalizeSettings.from_dict(d.get("personalize")),
            selection_translate=SelectionTranslateSettings.from_dict(
                d.get("selection_translate")
            ),
        )
