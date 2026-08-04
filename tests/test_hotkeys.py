"""快捷键测试：matcher 纯函数 + HotkeyService 边沿逻辑（注入 fake 事件源）。

测试目标 (AGENTS.md 13.1)：
- normalize_key_name：ctrl_l/ctrl_r->ctrl、alt_gr->alt、大小写。
- parse_hotkey：组合、单键、空 token 抛 ValueError。
- HotkeyService：
  * 长按自动重复只触发一次 on_press。
  * 松开边沿触发一次 on_release；组合键 ctrl+alt+f12 三键满配触发。
  * toggle_enabled / cancel 不触发 on_release。
  * update_bindings 后旧 binding 不再触发。

直接驱动 _on_key_down / _on_key_up（传字符串键名，绕过 pynput 对象）。
"""

from __future__ import annotations

import pytest

from dictashift.hotkeys.matcher import HotkeyBinding, normalize_key_name, parse_hotkey
from dictashift.hotkeys.service import HotkeyService, _PRESS_ONLY_ACTIONS


# ==================== normalize_key_name ====================


def test_normalize_ctrl_variants() -> None:
    assert normalize_key_name("ctrl_l") == "ctrl"
    assert normalize_key_name("ctrl_r") == "ctrl"
    assert normalize_key_name("Ctrl_L") == "ctrl"
    assert normalize_key_name("CTRL_R") == "ctrl"


def test_normalize_alt_variants() -> None:
    assert normalize_key_name("alt_l") == "alt"
    assert normalize_key_name("alt_r") == "alt"
    assert normalize_key_name("alt_gr") == "alt"
    # 大小写不敏感（pynput 给出的 .name 大小写可能不一致）
    assert normalize_key_name("ALT_GR") == "alt"
    assert normalize_key_name("Alt_L") == "alt"


def test_normalize_shift_cmd_variants() -> None:
    assert normalize_key_name("shift_l") == "shift"
    assert normalize_key_name("shift_r") == "shift"
    assert normalize_key_name("cmd_l") == "cmd"
    assert normalize_key_name("win_l") == "cmd"


def test_normalize_case_insensitive_and_plain() -> None:
    assert normalize_key_name("F8") == "f8"
    assert normalize_key_name("F12") == "f12"
    assert normalize_key_name("Space") == "space"
    assert normalize_key_name("A") == "a"
    # 已归一化输入保持不变
    assert normalize_key_name("ctrl") == "ctrl"
    assert normalize_key_name("f8") == "f8"


def test_normalize_empty_raises() -> None:
    with pytest.raises(ValueError):
        normalize_key_name("")
    with pytest.raises(ValueError):
        normalize_key_name("   ")


def test_normalize_none_raises() -> None:
    with pytest.raises(ValueError):
        normalize_key_name(None)  # type: ignore[arg-type]


# ==================== parse_hotkey ====================


def test_parse_hotkey_combo() -> None:
    keys = parse_hotkey("ctrl+alt+f12")
    assert keys == frozenset({"ctrl", "alt", "f12"})


def test_parse_hotkey_single() -> None:
    assert parse_hotkey("f8") == frozenset({"f8"})


def test_parse_hotkey_case_insensitive() -> None:
    assert parse_hotkey("Ctrl+Shift+F1") == frozenset({"ctrl", "shift", "f1"})


def test_parse_hotkey_dedup() -> None:
    """重复 token 去重。"""
    assert parse_hotkey("ctrl+ctrl+f8") == frozenset({"ctrl", "f8"})


def test_parse_hotkey_empty_token_raises() -> None:
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+")
    with pytest.raises(ValueError):
        parse_hotkey("ctrl++alt")
    with pytest.raises(ValueError):
        parse_hotkey("+f8")


def test_parse_hotkey_none_raises() -> None:
    with pytest.raises(ValueError):
        parse_hotkey(None)  # type: ignore[arg-type]


# ==================== HotkeyService（字符串驱动）====================


def _make_service(bindings, on_press=None, on_release=None):
    return HotkeyService(
        list(bindings),
        on_press or (lambda a: None),
        on_release or (lambda a: None),
    )


def _press(svc: HotkeyService, name: str) -> None:
    """直接驱动内部处理函数（传已归一化字符串键名）。"""
    svc._handle_press_locked(name)


def _release(svc: HotkeyService, name: str) -> None:
    svc._handle_release_locked(name)


def test_long_press_autorepeat_triggers_press_once() -> None:
    """重复 press 同一键（长按自动重复）只触发一次 on_press。"""
    presses: list[str] = []
    releases: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="dictate_zh", keys=frozenset({"f8"}))],
        on_press=presses.append,
        on_release=releases.append,
    )
    _press(svc, "f8")
    _press(svc, "f8")
    _press(svc, "f8")
    assert presses == ["dictate_zh"]


def test_release_edge_triggers_once() -> None:
    """松开边沿触发一次 on_release。"""
    presses: list[str] = []
    releases: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="dictate_zh", keys=frozenset({"f8"}))],
        on_press=presses.append,
        on_release=releases.append,
    )
    _press(svc, "f8")
    _release(svc, "f8")
    # 重复 release 不再触发
    _release(svc, "f8")
    assert releases == ["dictate_zh"]


def test_combo_three_keys_triggers_on_full() -> None:
    """ctrl+alt+f12：按下三个键才满配触发。"""
    presses: list[str] = []
    releases: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="toggle_enabled", keys=frozenset({"ctrl", "alt", "f12"}))],
        on_press=presses.append,
        on_release=releases.append,
    )
    _press(svc, "ctrl")
    assert presses == []
    _press(svc, "alt")
    assert presses == []
    _press(svc, "f12")
    assert presses == ["toggle_enabled"]


def test_toggle_and_cancel_no_release() -> None:
    """toggle_enabled / cancel 只触发 on_press，不触发 on_release。"""
    assert "toggle_enabled" in _PRESS_ONLY_ACTIONS
    assert "cancel" in _PRESS_ONLY_ACTIONS

    releases: list[str] = []
    svc = _make_service(
        [
            HotkeyBinding(action="toggle_enabled", keys=frozenset({"f1"})),
            HotkeyBinding(action="cancel", keys=frozenset({"f2"})),
        ],
        on_release=releases.append,
    )
    _press(svc, "f1")
    _release(svc, "f1")
    _press(svc, "f2")
    _release(svc, "f2")
    assert releases == []


def test_press_only_action_still_fires_press() -> None:
    """press-only action 的 on_press 仍正常触发。"""
    presses: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="cancel", keys=frozenset({"esc"}))],
        on_press=presses.append,
    )
    _press(svc, "esc")
    assert presses == ["cancel"]


def test_update_bindings_removes_old_binding() -> None:
    """update_bindings 后旧 binding 不再触发。"""
    presses: list[str] = []
    old = HotkeyBinding(action="dictate_zh", keys=frozenset({"f8"}))
    new = HotkeyBinding(action="dictate_zh", keys=frozenset({"f9"}))
    svc = _make_service([old], on_press=presses.append)

    # 更新为新绑定
    svc.update_bindings([new])

    # 按旧键不再触发
    _press(svc, "f8")
    assert presses == []
    # 按新键触发
    _press(svc, "f9")
    assert presses == ["dictate_zh"]


def test_partial_release_of_combo_fires_release() -> None:
    """组合键中任意构成键松开即不再满配 -> 触发一次 on_release（非 press-only）。"""
    releases: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="dictate_zh", keys=frozenset({"ctrl", "f8"}))],
        on_release=releases.append,
    )
    _press(svc, "ctrl")
    _press(svc, "f8")
    _release(svc, "ctrl")
    assert releases == ["dictate_zh"]


def test_double_start_raises_runtime_error() -> None:
    """重复 start 抛 RuntimeError（生命周期安全）。"""
    # _create_listener 默认创建真实 pynput Listener；
    # 这里覆盖为返回一个不真正启动的假对象，避免真实全局监听。
    svc = _make_service([HotkeyBinding(action="x", keys=frozenset({"f8"}))])

    class _FakeListener:
        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    svc._create_listener = lambda: _FakeListener()  # type: ignore[method-assign]
    svc.start()
    try:
        with pytest.raises(RuntimeError):
            svc.start()
    finally:
        svc.stop()


def test_selection_translate_is_press_only() -> None:
    """selection_translate（划词翻译）是 press-only action：只触发 on_press。"""
    assert "selection_translate" in _PRESS_ONLY_ACTIONS

    presses: list[str] = []
    releases: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="selection_translate", keys=frozenset({"f7"}))],
        on_press=presses.append,
        on_release=releases.append,
    )
    _press(svc, "f7")
    _release(svc, "f7")
    assert presses == ["selection_translate"]
    assert releases == []
