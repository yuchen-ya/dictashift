"""开机自启动（Windows 注册表）。

不变量 (AGENTS.md 11 / 12)：
- 仅依赖标准库 winreg，不引入第三方。
- 注册表位置固定：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，
  值名 DictaShift。
- 启用命令构造：
  * 打包模式（sys.frozen 为真）：sys.executable 绝对路径加引号
    （即 DictaShift.exe 自身）。
  * 开发模式（sys.frozen 假）：优先用 sys.executable 同目录的 pythonw.exe
    运行 `-m dictashift`（避免控制台窗口）；找不到 pythonw.exe 则退回
    python.exe -m dictashift。
- is_enabled：值存在且非空字符串视为启用。
- set_launch_at_login(False)：值不存在不报错；任何写入/删除失败抛 OSError。
"""

from __future__ import annotations

import sys
from pathlib import Path

import winreg  # type: ignore[import-not-found]

# 注册表位置：当前用户 Run 键，值名固定 DictaShift。
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "DictaShift"


def _build_command() -> str:
    """根据 sys.frozen 构造开机启动命令字符串。"""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        # 打包模式：直接以可执行文件自身启动。
        return f'"{exe}"'
    # 开发模式：优先 pythonw.exe（避免控制台窗口），找不到则退回 python.exe。
    pythonw = exe.with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else str(exe)
    return f'"{interpreter}" -m dictashift'


def is_launch_at_login_enabled() -> bool:
    """查询 Run\\DictaShift 是否存在且非空。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
    except FileNotFoundError:
        # 键或值不存在 -> 未启用。
        return False
    except OSError:
        # 其它注册表访问错误视为未启用（保守不抛）。
        return False
    return bool(value)


def set_launch_at_login(enabled: bool) -> None:
    """启用/禁用开机启动。

    enabled=True：写入构造好的命令字符串。
    enabled=False：删除值（不存在不报错）。
    失败抛 OSError（winreg 写入/删除/打开失败的基类）。
    """
    if enabled:
        command = _build_command()
        # OpenKey 若键不存在会抛 FileNotFoundError（OSError 子类）；Run 键在
        # 正常 Windows 上一定存在，但保险起见用 CreateKey 保证可写。
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command)
        return
    # 禁用：删除值。键或值不存在视为成功（幂等）。
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
    except FileNotFoundError:
        return
    # 其它 OSError 由调用方处理。
