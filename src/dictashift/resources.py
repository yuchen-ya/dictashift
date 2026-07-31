"""应用资源路径解析（开发版 / PyInstaller one-dir 兼容）。

打包版（one-dir）：sys._MEIPASS 指向解压后的 _internal 目录，spec datas 中的
assets/icons 原样落在其下；开发版：以仓库根为基准
（src/dictashift/resources.py -> parents[2]）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

# assets/icons 下的图标文件名（由 scripts/gen_icon.py 生成）。
_ICON_ICO_NAME = "dictashift.ico"
_ICON_PNG_NAME = "dictashift.png"


def resource_path(*parts: str) -> Path:
    """返回资源文件的绝对路径（不保证存在，调用方自行判空/回退）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        # PyInstaller 打包版：资源在 _internal 下。
        root = Path(base)
    else:
        # 开发版：仓库根。
        root = Path(__file__).resolve().parents[2]
    return root.joinpath(*parts)


def app_icon_ico_path() -> Path:
    """应用图标 .ico 路径（窗口/任务栏/托盘/EXE）。"""
    return resource_path("assets", "icons", _ICON_ICO_NAME)


def app_icon_png_path() -> Path:
    """应用图标 .png 路径（界面内 logo，如设置窗口 header）。"""
    return resource_path("assets", "icons", _ICON_PNG_NAME)


def load_app_icon() -> QIcon:
    """加载应用图标（.ico）；文件缺失时返回空 QIcon，由调用方决定回退策略。"""
    ico = app_icon_ico_path()
    if ico.exists():
        return QIcon(str(ico))
    return QIcon()
