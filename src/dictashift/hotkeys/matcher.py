"""快捷键定义与匹配逻辑（与 pynput 解耦，便于纯函数测试）。

本模块不 import pynput，只处理归一化后的字符串键名。
约定：所有归一化后的修饰键统一为左右不区分的单一名称：
    ctrl / alt / shift / cmd(Windows 键)
普通键名为小写形式，如 'f8'、'f12'、'a'、'1'、'space'。
"""

from dataclasses import dataclass


# 左右修饰键归一化映射：把 pynput 的 *_l / *_r 变体统一到无后缀修饰键。
_MOD_NORMALIZE = {
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "alt_gr": "alt",   # 兼容 pynput 在某些布局下的 AltGr 命名
    "shift_l": "shift",
    "shift_r": "shift",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "win_l": "cmd",
    "win_r": "cmd",
}


@dataclass(frozen=True)
class HotkeyBinding:
    """快捷键绑定。

    keys 为归一化后的键名 frozenset，如 frozenset({'f8'}) 或
    frozenset({'ctrl', 'alt', 'f12'})。action 由上层 controller 解释，
    约定取值："dictate_zh" | "dictate_en" | "toggle_enabled" | "cancel"。

    不变量：
    - keys 必须非空 frozenset，且每个元素都已通过 normalize_key_name 归一化。
    - HotkeyBinding 不可变，可作为运行时复制的稳定快照使用。
    """

    action: str
    keys: frozenset[str]


def normalize_key_name(name: str) -> str:
    """把单个 pynput 键名归一化为统一字符串。

    归一化规则（与 normalize_key_from_pynput 配合使用）：
    - 'ctrl_l'/'ctrl_r' -> 'ctrl'
    - 'alt_l'/'alt_r'/'alt_gr' -> 'alt'
    - 'shift_l'/'shift_r' -> 'shift'
    - 'cmd_l'/'cmd_r'/'win_l'/'win_r' -> 'cmd'
    - 其它已知的修饰/功能键名 (如 'f8'、'space'、'cmd') 保持原样并 lower()
    - 单字符 ('a'、'V'、'1') -> lower() 后的单字符

    已归一化的输入 (如 'ctrl'、'alt'、'f8') 再次调用保持不变。
    """
    if name is None:
        raise ValueError("normalize_key_name 不接受 None")
    s = name.strip().lower()
    if not s:
        raise ValueError("normalize_key_name 不接受空字符串")
    return _MOD_NORMALIZE.get(s, s)


def parse_hotkey(text: str) -> frozenset[str]:
    """解析配置字符串为归一化后的键名 frozenset。

    接受 "ctrl+alt+f12" / "f8" / "Ctrl+Shift+F1" 等形式，
    以 '+' 分割，每个 token 经 normalize_key_name 归一化。

    不变量：返回的 frozenset 非空；重复 token 会被去重。
    空字符串或纯空白 token 抛 ValueError。
    """
    if text is None:
        raise ValueError("parse_hotkey 不接受 None")
    parts = [p.strip() for p in text.split("+")]
    if not parts:
        raise ValueError(f"快捷键配置为空: {text!r}")
    # 含空白 token 视为非法配置 (例如 "ctrl+" 或 "ctrl++alt")，明确报错而非静默丢弃，
    # 避免用户误配置。
    if any(not p for p in parts):
        raise ValueError(f"快捷键配置包含空 token: {text!r}")
    return frozenset(normalize_key_name(p) for p in parts)
