"""矢量图标封装层：基于 qtawesome（MIT）+ Phosphor Icons 统一现代线条图标。

特点：
- 语义化图标名称映射到风格一致的 Phosphor（``ph.*``）图标集，同时支持直接传入
  原始 qtawesome 图标名（如 ``"ph.gear"``、``"mdi6.cog"``）。
- 安全兜底：无 QApplication、字体缺失、非法图标名或离屏环境下均不抛异常，
  安全返回空 ``QIcon`` / ``QPixmap``。
- 供设置窗口、托盘菜单、悬浮窗及 ``QuickLookupPopup`` 统一复用。

线程模型：仅在 UI 主线程调用（Qt 图标/像素图创建要求）。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

# 默认图标主色与次级色（与 theme.py 调色板保持一致，避免循环导入）。
DEFAULT_ICON_COLOR = "#F4F4F6"
MUTED_ICON_COLOR = "#9A9AA6"
DISABLED_ICON_COLOR = "#555560"

# 语义图标名 -> Phosphor (ph.*) 图标标识。
# 所有条目均已在当前环境的 qtawesome charmap 中验证可用。
ICON_MAP: dict[str, str] = {
    # 导航与通用
    "general": "ph.sliders-horizontal",
    "settings": "ph.gear",
    "hotkeys": "ph.keyboard",
    "asr": "ph.microphone",
    "mic": "ph.microphone",
    "translation": "ph.translate",
    "personalize": "ph.sparkle",
    "magic": "ph.magic-wand",
    # 常用动作
    "copy": "ph.copy",
    "check": "ph.check",
    "close": "ph.x",
    "clear": "ph.x",
    "erase": "ph.eraser",
    "add": "ph.plus",
    "delete": "ph.trash",
    "download": "ph.download-simple",
    "refresh": "ph.arrows-clockwise",
    "reset": "ph.arrow-clockwise",
    "test": "ph.flask",
    "popup": "ph.chat-centered-text",
    "search": "ph.magnifying-glass",
    "pin": "ph.push-pin",
    "pin-fill": "ph.push-pin-fill",
    "insert": "ph.arrow-elbow-down-left",
    "return": "ph.key-return",
    "globe": "ph.globe",
    "cpu": "ph.cpu",
    "sliders": "ph.faders",
    # 托盘与状态
    "power": "ph.power",
    "quit": "ph.sign-out",
    "warning": "ph.warning",
    "error": "ph.warning-circle",
    "bell": "ph.bell",
    "eye": "ph.eye",
    # 方向
    "caret-down": "ph.caret-down",
    "caret-up": "ph.caret-up",
}


def resolve_icon_name(name: str) -> str:
    """将语义名解析为 qtawesome 图标标识；已带前缀（含 '.'）则原样返回。"""
    if "." in name:
        return name
    return ICON_MAP.get(name, name)


def get_icon(
    name: str,
    color: str | None = None,
    size: int | None = None,
    *,
    color_active: str | None = None,
    color_disabled: str | None = None,
    scale_factor: float = 1.0,
) -> QIcon:
    """获取矢量 QIcon，失败时安全返回空 QIcon 而不抛异常。

    Args:
        name: 语义图标名（见 ``ICON_MAP``）或完整 qtawesome 图标名（如 ``"ph.gear"``）。
        color: 主色十六进制字符串，默认 ``DEFAULT_ICON_COLOR``。
        size: 可选目标像素尺寸；指定后会把图标光栅化为该尺寸的 ``QIcon``，
            否则返回 qtawesome 自适应缩放的 ``QIcon``。
        color_active: 激活/hover 态颜色（可选）。
        color_disabled: 禁用态颜色（默认 ``DISABLED_ICON_COLOR``）。
        scale_factor: 图标相对控件框的缩放因子。
    """
    if not name or QApplication.instance() is None:
        return QIcon()

    resolved = resolve_icon_name(name)
    if "." not in resolved:
        return QIcon()

    try:
        import qtawesome as qta

        opts: dict[str, Any] = {
            "color": color or DEFAULT_ICON_COLOR,
            "color_disabled": color_disabled or DISABLED_ICON_COLOR,
            "scale_factor": scale_factor,
        }
        if color_active:
            opts["color_active"] = color_active

        icon = qta.icon(resolved, **opts)
        if size is not None and size > 0 and not icon.isNull():
            pm = icon.pixmap(QSize(size, size))
            if not pm.isNull():
                return QIcon(pm)
        return icon
    except Exception:  # noqa: BLE001 - 离屏/缺字体/未知图标均优雅兜底
        logger.debug("[icons] 获取图标失败: name=%s resolved=%s", name, resolved, exc_info=True)
        return QIcon()


def get_pixmap(
    name: str,
    color: str | None = None,
    size: int = 16,
    *,
    scale_factor: float = 1.0,
) -> QPixmap:
    """便捷接口：按给定像素尺寸返回矢量图标的 ``QPixmap``，失败返回空 ``QPixmap``。"""
    icon = get_icon(name, color=color, scale_factor=scale_factor)
    if icon.isNull():
        return QPixmap()
    return icon.pixmap(QSize(size, size))
