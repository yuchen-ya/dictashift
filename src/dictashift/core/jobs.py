"""语音任务定义。

不变量 (AGENTS.md 5.4)：
- 每个语音任务在创建时冻结翻译配置快照。
- 用户切换模型只影响后续任务，不改变已开始任务的后端。
- dictate_zh 任务的 profile_snapshot 为 None（中文输入不经过翻译）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..translation import TranslationProfile


@dataclass
class VoiceTask:
    """一次语音输入任务。

    Attributes:
        action: 触发动作，"dictate_zh" 或 "dictate_en"。
        profile_snapshot: 任务创建时冻结的翻译配置快照。
            - dictate_zh: 恒为 None（中文直接输出，不翻译）。
            - dictate_en: 为任务创建时的 active profile 副本；
              用户后续切换只影响之后新建的任务。
    """

    action: str
    profile_snapshot: TranslationProfile | None
