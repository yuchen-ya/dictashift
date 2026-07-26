"""状态机 (AppState / StateMachine) 单元测试。

测试目标 (AGENTS.md 5.2)：
- 全部合法迁移逐条通过。
- 非法迁移抛 InvalidTransition，重点：
  * PREPARING 之外的任何状态 -> RECORDING 均非法（IDLE 须经 PREPARING）。
  * 任何状态不能直达 ERROR（除 TRANSCRIBING / TRANSLATING / INSERTING）。
  * IDLE -> ERROR 非法。
- force() 可任意跳转（绕过迁移表）。

本文件不依赖任何外部资源（无模型/麦克风/网络）。
"""

from __future__ import annotations

import pytest

from dictashift.core.state import AppState, InvalidTransition, StateMachine


# ==================== 合法迁移表（与 src/dictashift/core/state.py 对应）====================

LEGAL_TRANSITIONS: dict[AppState, list[AppState]] = {
    AppState.IDLE: [AppState.PREPARING, AppState.DISABLED],
    AppState.PREPARING: [AppState.RECORDING, AppState.IDLE, AppState.DISABLED],
    AppState.RECORDING: [AppState.TRANSCRIBING, AppState.IDLE, AppState.DISABLED],
    AppState.TRANSCRIBING: [
        AppState.TRANSLATING,
        AppState.INSERTING,
        AppState.IDLE,
        AppState.DISABLED,
        AppState.ERROR,
    ],
    AppState.TRANSLATING: [
        AppState.INSERTING,
        AppState.IDLE,
        AppState.DISABLED,
        AppState.ERROR,
    ],
    AppState.INSERTING: [AppState.IDLE, AppState.DISABLED, AppState.ERROR],
    AppState.ERROR: [AppState.IDLE, AppState.DISABLED],
    AppState.DISABLED: [AppState.IDLE],
}

ALL_STATES = [
    AppState.IDLE,
    AppState.PREPARING,
    AppState.RECORDING,
    AppState.TRANSCRIBING,
    AppState.TRANSLATING,
    AppState.INSERTING,
    AppState.ERROR,
    AppState.DISABLED,
]


# ==================== 合法迁移逐条 ====================


@pytest.mark.parametrize("start", ALL_STATES)
def test_each_legal_transition_succeeds(start: AppState) -> None:
    """从 start 出发的每一条合法迁移都应成功，并落到目标状态。"""
    for target in LEGAL_TRANSITIONS[start]:
        sm = StateMachine(initial=start)
        sm.transition(target)
        assert sm.state == target


def test_state_string_enum_values() -> None:
    """StrEnum 取值稳定（持久化/日志依赖）。"""
    assert AppState.IDLE == "idle"
    assert AppState.PREPARING == "preparing"
    assert AppState.RECORDING == "recording"
    assert AppState.TRANSCRIBING == "transcribing"
    assert AppState.TRANSLATING == "translating"
    assert AppState.INSERTING == "inserting"
    assert AppState.ERROR == "error"
    assert AppState.DISABLED == "disabled"


# ==================== 非法迁移 ====================


@pytest.mark.parametrize("start", ALL_STATES)
def test_each_illegal_transition_raises(start: AppState) -> None:
    """从 start 出发，除合法集合外的任何目标都应抛 InvalidTransition。"""
    legal = set(LEGAL_TRANSITIONS[start])
    for target in ALL_STATES:
        if target == start:
            continue  # self-transition 由 controller 层短路，state.transition 不在此处理
        if target in legal:
            continue
        sm = StateMachine(initial=start)
        with pytest.raises(InvalidTransition):
            sm.transition(target)
        # 失败后状态保持不变
        assert sm.state == start


def test_only_idle_can_enter_recording() -> None:
    """重点不变量：PREPARING 之外的任何状态 -> RECORDING 均非法
    （IDLE 也必须先经 PREPARING 再到 RECORDING）。"""
    for start in ALL_STATES:
        if start == AppState.PREPARING:
            continue
        sm = StateMachine(initial=start)
        with pytest.raises(InvalidTransition):
            sm.transition(AppState.RECORDING)
        assert sm.state == start


def test_direct_to_error_only_from_processing_states() -> None:
    """任何状态不能直达 ERROR，除 TRANSCRIBING / TRANSLATING / INSERTING 外。"""
    error_legal_from = {
        AppState.TRANSCRIBING,
        AppState.TRANSLATING,
        AppState.INSERTING,
    }
    for start in ALL_STATES:
        sm = StateMachine(initial=start)
        if start in error_legal_from:
            sm.transition(AppState.ERROR)
            assert sm.state == AppState.ERROR
        else:
            with pytest.raises(InvalidTransition):
                sm.transition(AppState.ERROR)
            assert sm.state == start


def test_idle_to_error_is_illegal() -> None:
    """IDLE -> ERROR 非法。"""
    sm = StateMachine(initial=AppState.IDLE)
    with pytest.raises(InvalidTransition):
        sm.transition(AppState.ERROR)
    assert sm.state == AppState.IDLE


def test_disabled_only_goes_to_idle() -> None:
    """DISABLED -> IDLE 合法；DISABLED -> 其它均非法。"""
    sm = StateMachine(initial=AppState.DISABLED)
    sm.transition(AppState.IDLE)
    assert sm.state == AppState.IDLE

    for target in ALL_STATES:
        if target in (AppState.IDLE, AppState.DISABLED):
            continue
        sm = StateMachine(initial=AppState.DISABLED)
        with pytest.raises(InvalidTransition):
            sm.transition(target)


def test_invalid_transition_is_runtime_error_subclass() -> None:
    """InvalidTransition 应为 RuntimeError 子类（便于 hotkey 回调兜底捕获）。"""
    assert issubclass(InvalidTransition, RuntimeError)


# ==================== force() ====================


@pytest.mark.parametrize("start", ALL_STATES)
@pytest.mark.parametrize("target", ALL_STATES)
def test_force_can_jump_anywhere(start: AppState, target: AppState) -> None:
    """force() 可从任意状态跳到任意状态，包括 self。"""
    sm = StateMachine(initial=start)
    sm.force(target)
    assert sm.state == target


def test_force_bypasses_transition_table() -> None:
    """force 走迁移表外的非法路径也不抛（IDLE -> ERROR 这种非法迁移）。"""
    sm = StateMachine(initial=AppState.IDLE)
    sm.force(AppState.ERROR)
    assert sm.state == AppState.ERROR


# ==================== 默认初值 ====================


def test_default_initial_state_is_idle() -> None:
    """StateMachine 默认初值为 IDLE。"""
    assert StateMachine().state == AppState.IDLE


def test_explicit_initial_state() -> None:
    """可显式指定初值。"""
    assert StateMachine(initial=AppState.DISABLED).state == AppState.DISABLED
