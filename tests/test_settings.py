"""配置数据模型与持久化仓库测试。

测试目标 (AGENTS.md 12)：
- AppSettings 默认值；to_dict/from_dict round-trip；from_dict 缺键用默认、忽略未知键。
- SettingsRepository（tmp_path）：
  * 不存在 -> 默认；
  * save -> load round-trip；
  * 损坏 JSON -> 备份 .bak + 返回默认；
  * 原子写无 .tmp 残留。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dictashift.settings import (
    AppSettings,
    AsrSettings,
    HotkeySettings,
    SettingsRepository,
    TranslationSettings,
    UiSettings,
)
from dictashift.settings.models import (
    profile_from_dict,
    profile_to_dict,
)
from dictashift.translation import TranslationProfile


# ==================== AppSettings 默认值 ====================


def test_app_settings_defaults() -> None:
    s = AppSettings()
    assert s.enabled is True
    assert isinstance(s.hotkeys, HotkeySettings)
    assert isinstance(s.asr, AsrSettings)
    assert isinstance(s.translation, TranslationSettings)
    assert isinstance(s.ui, UiSettings)
    # 子配置默认值
    assert s.hotkeys.dictate_zh == "f8"
    assert s.hotkeys.dictate_en == "f9"
    assert s.hotkeys.toggle_enabled == "ctrl+alt+f12"
    assert s.hotkeys.cancel == "esc"
    assert s.asr.max_record_seconds == 60
    assert s.asr.mic_device_index is None
    assert s.translation.active_profile == ""
    assert s.translation.profiles == []
    assert s.ui.result_ui == "popup"
    assert s.ui.launch_at_login is False


# ==================== to_dict / from_dict round-trip ====================


def test_app_settings_round_trip() -> None:
    s = AppSettings(
        enabled=False,
        hotkeys=HotkeySettings(dictate_zh="f1", dictate_en="f2", toggle_enabled="ctrl+a", cancel="esc"),
        asr=AsrSettings(max_record_seconds=30, mic_device_index=2),
        translation=TranslationSettings(
            active_profile="p1",
            profiles=[
                TranslationProfile(name="p1", base_url="http://x/v1", model="m", api_key="k", temperature=0.5),
            ],
        ),
        ui=UiSettings(result_ui="overlay", launch_at_login=True),
    )
    d = s.to_dict()
    s2 = AppSettings.from_dict(d)
    assert s2 == s


def test_profile_to_from_dict_round_trip() -> None:
    p = TranslationProfile(name="n", base_url="http://b/v1", model="m", api_key="k", temperature=0.4, timeout=12.0)
    d = profile_to_dict(p)
    p2 = profile_from_dict(d)
    assert p2 == p


# ==================== from_dict 缺键 / 忽略未知键 ====================


def test_from_dict_missing_keys_uses_defaults() -> None:
    s = AppSettings.from_dict({"enabled": False})
    assert s.enabled is False
    # 其余子配置走默认
    assert s.hotkeys.dictate_zh == "f8"
    assert s.asr.max_record_seconds == 60
    assert s.translation.active_profile == ""
    assert s.ui.result_ui == "popup"


def test_from_dict_none_uses_defaults() -> None:
    assert AppSettings.from_dict(None) == AppSettings()
    assert AppSettings.from_dict({}) == AppSettings()


def test_from_dict_ignores_unknown_keys() -> None:
    d = AppSettings().to_dict()
    d["unknown_top"] = 1
    d["hotkeys"]["unknown_sub"] = 2
    s = AppSettings.from_dict(d)
    # 未知键被忽略，已知键正常还原
    assert s.enabled is True
    assert s.hotkeys.dictate_zh == "f8"


# ==================== SettingsRepository ====================


def test_repo_load_missing_returns_default(tmp_path: Path) -> None:
    repo = SettingsRepository(path=tmp_path / "settings.json")
    assert not repo.path.exists()
    assert repo.load() == AppSettings()


def test_repo_save_load_round_trip(tmp_path: Path) -> None:
    repo = SettingsRepository(path=tmp_path / "settings.json")
    s = AppSettings(
        enabled=False,
        hotkeys=HotkeySettings(dictate_zh="f1"),
        asr=AsrSettings(max_record_seconds=20),
    )
    repo.save(s)
    assert repo.path.exists()
    s2 = repo.load()
    assert s2 == s


def test_repo_load_corrupt_backups_and_returns_default(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{ not valid json", encoding="utf-8")
    repo = SettingsRepository(path=p)
    result = repo.load()
    assert result == AppSettings()
    # 损坏文件被备份为 .bak
    assert p.with_suffix(".json.bak").exists()


def test_repo_load_non_dict_json_backups(tmp_path: Path) -> None:
    """顶层非 dict（如数组）视为损坏 -> 备份 + 默认。"""
    p = tmp_path / "settings.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    repo = SettingsRepository(path=p)
    assert repo.load() == AppSettings()
    assert p.with_suffix(".json.bak").exists()


def test_repo_save_atomic_no_tmp_residual(tmp_path: Path) -> None:
    """原子写后无 .tmp 残留。"""
    p = tmp_path / "sub" / "settings.json"
    repo = SettingsRepository(path=p)
    repo.save(AppSettings())
    assert p.exists()
    # 父目录应被创建
    assert p.parent.exists()
    # 无 .tmp 残留
    assert not p.with_suffix(".json.tmp").exists()


# ==================== AsrSettings 旧配置迁移 ====================


def test_asr_settings_no_device_fields() -> None:
    """AsrSettings 不再有 device/device_id 字段（GPU 路线已移除）。"""
    a = AsrSettings()
    assert not hasattr(a, "device")
    assert not hasattr(a, "device_id")
    # to_dict 不应包含这些键
    assert "device" not in a.to_dict()
    assert "device_id" not in a.to_dict()


def test_asr_settings_ignores_legacy_device_keys() -> None:
    """旧配置文件中的 device/device_id 键被忽略（from_dict 不读取）。"""
    a = AsrSettings.from_dict({"device": "cuda", "device_id": 3, "max_record_seconds": 20})
    assert a.max_record_seconds == 20
    assert not hasattr(a, "device")
    # round-trip 不再写出 device/device_id
    assert "device" not in a.to_dict()


def test_asr_settings_round_trip_no_device() -> None:
    """AsrSettings 序列化往返不含 device。"""
    a = AsrSettings(max_record_seconds=45, mic_device_index=1)
    a2 = AsrSettings.from_dict(a.to_dict())
    assert a2 == a
    assert "device" not in a2.to_dict()


# ==================== TranslationSettings 迁移（内置免登录服务） ====================


def test_translation_settings_filters_free_profiles() -> None:
    """旧版本保存的免费 profile（backend_type != openai_compatible）被过滤掉。"""
    d = {
        "active_profile": "",
        "profiles": [
            {"name": "Google 免费", "backend_type": "google"},
            {"name": "Bing 免费", "backend_type": "bing"},
            {"name": "myLLM", "backend_type": "openai_compatible",
             "base_url": "http://x/v1", "model": "m"},
        ],
    }
    t = TranslationSettings.from_dict(d)
    assert [p.name for p in t.profiles] == ["myLLM"]
    assert all(p.backend_type == "openai_compatible" for p in t.profiles)


def test_translation_settings_active_remapped_to_builtin() -> None:
    """active 指向被过滤的免费 profile 时，按 backend_type 映射到内置项 name。"""
    from dictashift.translation import BUILTIN_GOOGLE, BUILTIN_BING

    # active 指向 google 免费 profile
    d = {
        "active_profile": "Google 免费",
        "profiles": [{"name": "Google 免费", "backend_type": "google"}],
    }
    t = TranslationSettings.from_dict(d)
    assert t.active_profile == BUILTIN_GOOGLE.name

    # active 指向 bing 免费 profile
    d2 = {
        "active_profile": "Bing 免费",
        "profiles": [{"name": "Bing 免费", "backend_type": "bing"}],
    }
    t2 = TranslationSettings.from_dict(d2)
    assert t2.active_profile == BUILTIN_BING.name


def test_translation_settings_active_nonexistent_becomes_empty() -> None:
    """active 指向不存在的 name（既非保留项也非内置项）-> ''。"""
    d = {
        "active_profile": "ghost",
        "profiles": [{"name": "a", "backend_type": "openai_compatible"}],
    }
    t = TranslationSettings.from_dict(d)
    assert t.active_profile == ""


def test_translation_settings_active_preserved_when_exists() -> None:
    """active 指向保留的用户 profile 时保持不变。"""
    d = {
        "active_profile": "myLLM",
        "profiles": [{"name": "myLLM", "backend_type": "openai_compatible",
                      "base_url": "http://x/v1", "model": "m"}],
    }
    t = TranslationSettings.from_dict(d)
    assert t.active_profile == "myLLM"


def test_translation_settings_round_trip_after_migration() -> None:
    """迁移后再往返：仅含 openai_compatible profile，active 可为内置 name。"""
    from dictashift.translation import BUILTIN_GOOGLE

    # 模拟旧配置 -> 迁移
    d = {
        "active_profile": "Google 免费",
        "profiles": [
            {"name": "Google 免费", "backend_type": "google"},
            {"name": "myLLM", "backend_type": "openai_compatible",
             "base_url": "http://x/v1", "model": "m", "api_key": "k"},
        ],
    }
    t = TranslationSettings.from_dict(d)
    assert t.active_profile == BUILTIN_GOOGLE.name
    # 再 to_dict -> from_dict 应稳定（active 仍为内置 name，profiles 不变）
    d2 = t.to_dict()
    t2 = TranslationSettings.from_dict(d2)
    assert t2 == t
    assert t2.active_profile == BUILTIN_GOOGLE.name
    assert [p.name for p in t2.profiles] == ["myLLM"]


# ==================== UiSettings result_ui 迁移 ====================


def test_ui_settings_legacy_show_overlay_true_migrates_to_overlay() -> None:
    """旧键 show_overlay=True -> result_ui="overlay"。"""
    u = UiSettings.from_dict({"show_overlay": True})
    assert u.result_ui == "overlay"


def test_ui_settings_legacy_show_overlay_false_migrates_to_popup() -> None:
    """旧键 show_overlay=False -> result_ui="popup"。"""
    u = UiSettings.from_dict({"show_overlay": False})
    assert u.result_ui == "popup"


def test_ui_settings_missing_result_ui_and_show_overlay_defaults_popup() -> None:
    """既无 result_ui 也无 show_overlay -> 默认 popup。"""
    u = UiSettings.from_dict({"launch_at_login": True})
    assert u.result_ui == "popup"
    assert u.launch_at_login is True


def test_ui_settings_invalid_result_ui_normalizes_to_popup() -> None:
    """非法 result_ui 值 -> 归一 popup。"""
    u = UiSettings.from_dict({"result_ui": "weird"})
    assert u.result_ui == "popup"


def test_ui_settings_valid_result_ui_preserved() -> None:
    """合法 result_ui 直接保留。"""
    assert UiSettings.from_dict({"result_ui": "overlay"}).result_ui == "overlay"
    assert UiSettings.from_dict({"result_ui": "popup"}).result_ui == "popup"


def test_ui_settings_to_dict_omits_show_overlay() -> None:
    """to_dict 只写 result_ui，不写旧键 show_overlay。"""
    d = UiSettings(result_ui="overlay").to_dict()
    assert d == {"result_ui": "overlay", "launch_at_login": False}
    assert "show_overlay" not in d


def test_ui_settings_round_trip() -> None:
    """result_ui 往返稳定。"""
    u = UiSettings(result_ui="overlay", launch_at_login=True)
    assert UiSettings.from_dict(u.to_dict()) == u

