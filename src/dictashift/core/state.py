"""应用运行时状态机。

不变量 (AGENTS.md 5.2)：
- 只有 IDLE 可进入 PREPARING；只有 PREPARING 可进入 RECORDING。
- DISABLED 状态不启动录音、不调用模型、不修改剪贴板。
- 全局切换快捷键在 DISABLED 状态仍可恢复启用（DISABLED -> IDLE）。
- 录音中禁用时停止并丢弃本次录音（RECORDING -> DISABLED）。
- 推理中禁用时让后台任务自然结束，但最终不得粘贴结果
  （状态层面允许 TRANSCRIBING/TRANSLATING -> DISABLED 迁移）。

合法迁移表（严格实现）：
- IDLE        -> PREPARING, DISABLED
- PREPARING   -> RECORDING, IDLE, DISABLED（打开麦克风失败回 IDLE；准备中禁用）
- RECORDING   -> TRANSCRIBING, IDLE, DISABLED
- TRANSCRIBING-> TRANSLATING, REWRITING, INSERTING, IDLE, DISABLED, ERROR
- TRANSLATING -> REWRITING, INSERTING, IDLE, DISABLED, ERROR
- REWRITING   -> INSERTING, IDLE, DISABLED, ERROR
- INSERTING   -> IDLE, DISABLED, ERROR
- ERROR       -> IDLE, DISABLED
- DISABLED    -> IDLE
- 任何状态 -> ERROR 不允许（ERROR 只能从上表列出的状态进入）

PREPARING 语义：按下听写快捷键后、麦克风流真正打开之前的过渡状态，
让 UI 能如实显示「准备中…」而不是提前声称「录音中」。
"""

from __future__ import annotations

import logging
import threading
from enum import StrEnum

logger = logging.getLogger(__name__)


class AppState(StrEnum):
    """应用运行时状态。"""

    DISABLED = "disabled"
    IDLE = "idle"
    PREPARING = "preparing"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    TRANSLATING = "translating"
    REWRITING = "rewriting"
    INSERTING = "inserting"
    ERROR = "error"


class InvalidTransition(RuntimeError):
    """非法状态迁移。"""


# ==================== 合法迁移表 ====================
#
# value 为从该状态允许迁移到的目标状态集合。
# 注意：ERROR 只能从 TRANSCRIBING / TRANSLATING / INSERTING 进入。
_TRANSITIONS: dict[AppState, frozenset[AppState]] = {
    AppState.IDLE: frozenset({AppState.PREPARING, AppState.DISABLED}),
    AppState.PREPARING: frozenset({AppState.RECORDING, AppState.IDLE, AppState.DISABLED}),
    AppState.RECORDING: frozenset(
        {AppState.TRANSCRIBING, AppState.IDLE, AppState.DISABLED}
    ),
    AppState.TRANSCRIBING: frozenset(
        {AppState.TRANSLATING, AppState.REWRITING, AppState.INSERTING, AppState.IDLE, AppState.DISABLED, AppState.ERROR}
    ),
    AppState.TRANSLATING: frozenset(
        {AppState.REWRITING, AppState.INSERTING, AppState.IDLE, AppState.DISABLED, AppState.ERROR}
    ),
    AppState.REWRITING: frozenset(
        {AppState.INSERTING, AppState.IDLE, AppState.DISABLED, AppState.ERROR}
    ),
    AppState.INSERTING: frozenset({AppState.IDLE, AppState.DISABLED, AppState.ERROR}),
    AppState.ERROR: frozenset({AppState.IDLE, AppState.DISABLED}),
    AppState.DISABLED: frozenset({AppState.IDLE}),
}


class StateMachine:
    """线程安全的状态机。

    - transition(target)：按合法迁移表迁移；非法迁移抛 InvalidTransition。
    - force(target)：绕过迁移表强制设置；仅用于 ERROR 恢复等显式场景，记日志。
    """

    def __init__(self, initial: AppState = AppState.IDLE) -> None:
        self._lock = threading.Lock()
        self._state: AppState = initial

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    def transition(self, target: AppState) -> None:
        """按合法迁移表迁移到 target。非法迁移抛 InvalidTransition。"""
        with self._lock:
            current = self._state
            allowed = _TRANSITIONS.get(current, frozenset())
            if target not in allowed:
                raise InvalidTransition(
                    f"非法状态迁移: {current} -> {target}"
                )
            self._state = target

    def force(self, target: AppState) -> None:
        """强制设置状态，绕过迁移表。

        仅用于 ERROR 恢复或中断等显式场景；每次调用都记 warning 日志，
        便于审计异常路径。
        """
        with self._lock:
            current = self._state
            self._state = target
        logger.warning(
            "[StateMachine] force 迁移: %s -> %s (绕过迁移表)", current, target
        )
