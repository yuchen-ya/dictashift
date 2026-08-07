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


# ==================== 空名称按键静默处理（修复 B）====================
#
# 某些按键（如多媒体键）经 pynput 给出空名称。旧行为：_normalize_from_pynput
# 调 normalize_key_name(空串) -> ValueError -> _on_key_down/_on_key_up 的 except
# -> logger.exception 刷 ERROR 日志。修复后：空/纯空白名称提前返回 None，事件
# 静默跳过（最多 debug 日志），不抛错、不产生 ERROR 日志。
#
# 不改 normalize_key_name 对配置校验的既有契约（空串仍抛 ValueError，见
# test_normalize_empty_raises / test_parse_hotkey_empty_token_raises）。


class _FakePynputKey:
    """模拟 pynput.keyboard.Key（有 .name 属性）。"""

    def __init__(self, name) -> None:
        self.name = name


class _FakePynputKeyCode:
    """模拟 pynput.keyboard.KeyCode（有 .char 属性）。"""

    def __init__(self, char) -> None:
        self.char = char


def test_normalize_from_pynput_empty_name_returns_none() -> None:
    """空/纯空白 .name 的 Key 经 _normalize_from_pynput 返回 None。"""
    from dictashift.hotkeys.service import _normalize_from_pynput

    assert _normalize_from_pynput(_FakePynputKey("")) is None
    assert _normalize_from_pynput(_FakePynputKey("   ")) is None
    assert _normalize_from_pynput(_FakePynputKey(None)) is None


def test_normalize_from_pynput_empty_char_returns_none() -> None:
    """空/纯空白 .char 的 KeyCode 经 _normalize_from_pynput 返回 None。"""
    from dictashift.hotkeys.service import _normalize_from_pynput

    assert _normalize_from_pynput(_FakePynputKeyCode("")) is None
    assert _normalize_from_pynput(_FakePynputKeyCode("   ")) is None
    assert _normalize_from_pynput(_FakePynputKeyCode(None)) is None


def test_normalize_from_pynput_empty_string_returns_none() -> None:
    """空/纯白字符串兜底分支返回 None。"""
    from dictashift.hotkeys.service import _normalize_from_pynput

    assert _normalize_from_pynput("") is None
    assert _normalize_from_pynput("   ") is None


def test_normalize_from_pynput_normal_string_unaffected() -> None:
    """对照：正常字符串经 _normalize_from_pynput 仍正常归一化（回归保护）。

    注：Key/KeyCode 分支需真实 pynput 对象（isinstance 判定），无法用 duck-type
    fake 触发；正常按键路径已由 HotkeyService 字符串驱动测试全覆盖。
    这里只验证裸字符串兜底分支（pynput 极少数情况给出裸字符串）。
    """
    from dictashift.hotkeys.service import _normalize_from_pynput

    assert _normalize_from_pynput("ctrl_l") == "ctrl"
    assert _normalize_from_pynput("f8") == "f8"
    assert _normalize_from_pynput("a") == "a"


def test_empty_name_key_event_silent_no_error_log(caplog) -> None:
    """空名称按键事件经 _on_key_down / _on_key_up 静默跳过：不抛错、不产 ERROR 日志。

    直接驱动 service._on_key_down / _on_key_up（与 pynput 回调路径一致），
    传空名称的 fake Key/KeyCode 对象。用 caplog 断言无 ERROR 级别日志。
    """
    import logging

    presses: list[str] = []
    releases: list[str] = []
    svc = _make_service(
        [HotkeyBinding(action="dictate_zh", keys=frozenset({"f8"}))],
        on_press=presses.append,
        on_release=releases.append,
    )

    with caplog.at_level(logging.DEBUG, logger="dictashift.hotkeys.service"):
        # 空名称 Key 的按下/松开。
        svc._on_key_down(_FakePynputKey(""))
        svc._on_key_up(_FakePynputKey("   "))
        # 空名称 KeyCode 的按下/松开。
        svc._on_key_down(_FakePynputKeyCode(""))
        svc._on_key_up(_FakePynputKeyCode(None))

    # 没有任何回调被触发（空名称事件被忽略）。
    assert presses == []
    assert releases == []
    # 没有 ERROR 级别日志（旧行为会刷 ValueError 异常堆栈）。
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == [], f"期望无 ERROR 日志，实际 {error_records}"


def test_normalize_key_name_still_rejects_empty_for_config() -> None:
    """对照：normalize_key_name 对空串仍抛 ValueError（配置校验契约不变）。

    _normalize_from_pynput 的空名称哨兵只放宽了运行时按键事件路径，
    不影响 parse_hotkey/normalize_key_name 对配置字符串的严格校验。
    """
    with pytest.raises(ValueError):
        normalize_key_name("")
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+")

