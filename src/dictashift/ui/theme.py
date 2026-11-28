"""集中式现代暗色设计系统（Shadcn / Raycast 风格）：Token 常量 + 全局 QSS。

设计原则：
- 深邃低噪点的暗色层级：画布 `#0A0A0D` -> 侧栏 `#0E0E12` -> 卡片 `#141418` -> 抬升 `#1C1C22`。
- 细腻 1px 半透明描边 `rgba(255, 255, 255, 0.09)` + 清晰对比度文字 `#F4F4F6` / `#9A9AA6`。
- 现代柔和紫色主高亮 `#7C6CF6`（hover `#8F80FF` / pressed `#6856E8`）。
- 全局应用级 `app_stylesheet()` 覆盖主窗口、弹窗、托盘 QMenu、QMessageBox、QToolTip、
  滚动条、输入框、按钮层级（primary/secondary/ghost/danger/icon）以及自定义控件样式。
- 运行时用 QPainter 生成高 DPI 抗锯齿 PNG 箭头与对勾资产，彻底修复 Qt Widgets
  暗色 QSS 下 `QComboBox` 下拉箭头不可见与 `QCheckBox` 选中无对勾问题，且无需 QtSvg。
- 统一悬浮窗样式构建器 `popup_card_stylesheet()`，供 `StateOverlay`、`ResultPopup`、
  `SelectionPopup` 及 `QuickLookupPopup` 共用。

线程模型：Token 常量无线程约束；QSS 图标资产由纯离屏 QImage/QPainter 生成。
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

# ==================== 设计系统 Token ====================

# 层级背景（由深到浅）
BG_WINDOW = "#0A0A0D"          # 主画布底色
BG_SIDEBAR = "#0E0E12"         # 侧边导航栏 / 底栏
BG_SURFACE = "#141418"         # 卡片容器底色
BG_ELEVATED = "#1C1C22"        # 抬升表面（菜单/下拉列表/Tooltip/弹窗内块）
BG_INPUT = "#18181E"           # 输入框 / 控件底色
BG_HOVER = "#23232C"           # 悬停提亮
BG_ACTIVE = "#2B2B36"          # 按下态 / 柔和选中底
BG_RECESSED = "#101014"        # 凹槽底（分段控件底托 / 进度条轨道）

# 描边与分割线
BORDER = "rgba(255, 255, 255, 0.09)"
BORDER_SUBTLE = "rgba(255, 255, 255, 0.06)"
BORDER_STRONG = "rgba(255, 255, 255, 0.16)"
BORDER_SOLID = "#27272F"       # 不支持 alpha 或需实色兜底处使用

# 文字对比度层级
TEXT_PRIMARY = "#F4F4F6"       # 主文字
TEXT_SECONDARY = "#9A9AA6"     # 副标题 / 次级说明
TEXT_MUTED = "#71717E"         # 弱提示 / 占位符
TEXT_DISABLED = "#555560"      # 禁用文字

# 主题高亮（Violet Accent）
ACCENT = "#7C6CF6"
ACCENT_LIGHT = "#8F80FF"       # hover
ACCENT_DARK = "#6856E8"        # pressed
ACCENT_SOFT = "rgba(124, 108, 246, 0.16)"
ACCENT_SOFT_HOVER = "rgba(124, 108, 246, 0.24)"
ACCENT_BORDER = "rgba(124, 108, 246, 0.50)"

# 语义状态色
SUCCESS = "#22C55E"
SUCCESS_SOFT = "rgba(34, 197, 94, 0.14)"
SUCCESS_BORDER = "rgba(34, 197, 94, 0.35)"

WARNING = "#F59E0B"
WARNING_SOFT = "rgba(245, 158, 11, 0.14)"
WARNING_BORDER = "rgba(245, 158, 11, 0.35)"

DANGER = "#EF4444"
DANGER_TEXT = "#F87171"
DANGER_SOFT = "rgba(239, 68, 68, 0.12)"
DANGER_HOVER = "rgba(239, 68, 68, 0.22)"
DANGER_BORDER = "rgba(239, 68, 68, 0.42)"

INFO = "#38BDF8"

# 圆角 Token（像素）
RADIUS_SM = 6
RADIUS_MD = 8
RADIUS_LG = 12

# 字体栈
FONT_FAMILY = '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'

# ==================== 悬浮窗共用 Token ====================
# 供 StateOverlay / ResultPopup / SelectionPopup / QuickLookupPopup 共享。
CARD_BG = "rgba(18, 18, 22, 232)"
CARD_BG_POPUP = "rgba(20, 20, 25, 242)"
CARD_TEXT = TEXT_PRIMARY
POPUP_BORDER = "rgba(255, 255, 255, 0.11)"
POPUP_TEXT_BG = "#111116"
POPUP_TEXT_BORDER = "rgba(255, 255, 255, 0.08)"
POPUP_TEXT_COLOR = TEXT_PRIMARY
# 兼容旧导出名（历史上指向浅底深字，现统一为暗色可读文字色）
POPUP_TEXT_DARK = TEXT_PRIMARY
POPUP_BTN_BG = "rgba(255, 255, 255, 0.06)"
POPUP_BTN_BORDER = "rgba(255, 255, 255, 0.12)"
POPUP_BTN_HOVER = "rgba(255, 255, 255, 0.12)"
POPUP_BTN_PRESSED = "rgba(255, 255, 255, 0.18)"


# ==================== 运行时 QSS 矢量小图标生成 ====================


@lru_cache(maxsize=1)
def _ensure_qss_indicator_assets() -> dict[str, str]:
    """用 QImage + QPainter 生成高 DPI 抗锯齿 PNG（对勾/上下箭头），供 QSS url() 引用。

    返回 ``{"check": posix_path, "chevron_down": ..., "chevron_up": ...}``；
    异常时返回空字典（降级为无图标而不崩溃）。
    """
    try:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

        asset_dir = Path(tempfile.gettempdir()) / "dictashift_ui_assets_v1"
        asset_dir.mkdir(parents=True, exist_ok=True)

        def _render(filename: str, draw_fn) -> str:
            path = asset_dir / filename
            img = QImage(32, 32, QImage.Format.Format_ARGB32)
            img.fill(Qt.GlobalColor.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            draw_fn(painter)
            painter.end()
            img.save(str(path), "PNG")
            return path.as_posix()

        def _draw_check(p: QPainter) -> None:
            pen = QPen(QColor("#FFFFFF"), 3.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(QPointF(7.5, 16.5))
            path.lineTo(QPointF(13.5, 22.5))
            path.lineTo(QPointF(24.5, 9.5))
            p.drawPath(path)

        def _draw_chevron_down(p: QPainter) -> None:
            pen = QPen(QColor(TEXT_SECONDARY), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(QPointF(8.5, 12.5))
            path.lineTo(QPointF(16.0, 20.0))
            path.lineTo(QPointF(23.5, 12.5))
            p.drawPath(path)

        def _draw_chevron_up(p: QPainter) -> None:
            pen = QPen(QColor(TEXT_SECONDARY), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(QPointF(8.5, 19.5))
            path.lineTo(QPointF(16.0, 12.0))
            path.lineTo(QPointF(23.5, 19.5))
            p.drawPath(path)

        return {
            "check": _render("check_white.png", _draw_check),
            "chevron_down": _render("chevron_down.png", _draw_chevron_down),
            "chevron_up": _render("chevron_up.png", _draw_chevron_up),
        }
    except Exception:  # noqa: BLE001
        return {}


# ==================== 悬浮窗共用 QSS 构造 ====================


def popup_card_stylesheet(
    card_id: str = "popupCard",
    *,
    bg: str = CARD_BG_POPUP,
    radius: int = RADIUS_LG,
    text_bg: str | None = POPUP_TEXT_BG,
    text_border: str | None = POPUP_TEXT_BORDER,
    compact_buttons: bool = False,
) -> str:
    """生成悬浮卡片统一 QSS（供 ResultPopup / SelectionPopup / QuickLookupPopup 复用）。

    Args:
        card_id: 卡片根容器 objectName。
        bg: 卡片背景色。
        radius: 卡片圆角像素。
        text_bg: 内部 QTextEdit 底色；传 ``None`` 表示透明无框（如 SelectionPopup）。
        text_border: 内部 QTextEdit 描边色；``None`` 时无边框。
        compact_buttons: True 使用更紧凑的按钮内边距（如划词小浮卡）。
    """
    if text_bg is None:
        text_rule = (
            f"QTextEdit {{ background: transparent; border: none; color: {POPUP_TEXT_COLOR};"
            f" selection-background-color: {ACCENT}; }}"
        )
    else:
        border_css = f"1px solid {text_border}" if text_border else "none"
        text_rule = (
            f"QTextEdit {{ background-color: {text_bg}; color: {POPUP_TEXT_COLOR};"
            f" border: {border_css}; border-radius: {RADIUS_MD}px; padding: 6px 8px;"
            f" selection-background-color: {ACCENT}; }}"
        )

    btn_pad = "2px 9px" if compact_buttons else "5px 12px"
    btn_radius = 6 if compact_buttons else 7
    btn_font = "12px" if compact_buttons else "12.5px"

    return f"""
#{card_id} {{
    background-color: {bg};
    border: 1px solid {POPUP_BORDER};
    border-radius: {radius}px;
}}
#{card_id} QLabel {{
    color: {CARD_TEXT};
    background: transparent;
}}
#{card_id} QLabel#popupSubtitle {{
    color: {TEXT_SECONDARY};
    font-size: 12px;
}}
{text_rule}
#{card_id} QLineEdit {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
}}
#{card_id} QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
#{card_id} QScrollBar:vertical {{
    width: 6px;
    background: transparent;
    margin: 2px 0;
}}
#{card_id} QScrollBar::handle:vertical {{
    background: rgba(255, 255, 255, 0.20);
    border-radius: 3px;
    min-height: 20px;
}}
#{card_id} QScrollBar::handle:vertical:hover {{
    background: rgba(255, 255, 255, 0.34);
}}
#{card_id} QScrollBar::add-line:vertical, #{card_id} QScrollBar::sub-line:vertical,
#{card_id} QScrollBar::add-page:vertical, #{card_id} QScrollBar::sub-page:vertical {{
    height: 0;
    background: none;
}}
#{card_id} QPushButton {{
    background-color: {POPUP_BTN_BG};
    color: {CARD_TEXT};
    border: 1px solid {POPUP_BTN_BORDER};
    border-radius: {btn_radius}px;
    padding: {btn_pad};
    font-size: {btn_font};
    font-weight: 500;
}}
#{card_id} QPushButton:hover {{
    background-color: {POPUP_BTN_HOVER};
    border-color: rgba(255, 255, 255, 0.22);
}}
#{card_id} QPushButton:pressed {{
    background-color: {POPUP_BTN_PRESSED};
}}
#{card_id} QPushButton[variant="primary"] {{
    background-color: {ACCENT};
    color: #FFFFFF;
    border: 1px solid {ACCENT};
}}
#{card_id} QPushButton[variant="primary"]:hover {{
    background-color: {ACCENT_LIGHT};
    border-color: {ACCENT_LIGHT};
}}
#{card_id} QPushButton[variant="ghost"] {{
    background-color: transparent;
    border-color: transparent;
    color: {TEXT_SECONDARY};
}}
#{card_id} QPushButton[variant="ghost"]:hover {{
    background-color: rgba(255, 255, 255, 0.08);
    color: {TEXT_PRIMARY};
}}
"""


# ==================== 全局应用级 QSS ====================


def app_stylesheet() -> str:
    """生成全局应用级现代暗色 QSS（Shadcn / Raycast 风格）。"""
    assets = _ensure_qss_indicator_assets()
    check_url = f'image: url("{assets["check"]}");' if "check" in assets else ""
    down_url = f'image: url("{assets["chevron_down"]}");' if "chevron_down" in assets else ""
    up_url = f'image: url("{assets["chevron_up"]}");' if "chevron_up" in assets else ""

    return f"""
/* ==================== 基础全局排版与画布 ==================== */
QWidget {{
    background-color: {BG_WINDOW};
    color: {TEXT_PRIMARY};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}

QLabel {{
    background: transparent;
    color: {TEXT_PRIMARY};
}}

QWidget#transparentWrap, QWidget#hotkeyEdit {{
    background-color: transparent;
}}

/* ==================== 对话框与消息框 ==================== */
QDialog, QMessageBox {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
}}
QMessageBox QLabel {{
    color: {TEXT_PRIMARY};
    background: transparent;
    font-size: 13px;
}}
QMessageBox QPushButton {{
    min-width: 72px;
    padding: 6px 14px;
}}

/* ==================== 系统托盘与上下文菜单 ==================== */
QMenu {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 5px;
}}
QMenu::item {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    padding: 7px 22px 7px 12px;
    border-radius: {RADIUS_SM}px;
    margin: 1px 0;
}}
QMenu::item:selected {{
    background-color: {ACCENT_SOFT};
    color: #FFFFFF;
}}
QMenu::item:disabled {{
    color: {TEXT_DISABLED};
}}
QMenu::separator {{
    height: 1px;
    background-color: {BORDER};
    margin: 5px 8px;
}}
QMenu::icon {{
    padding-left: 4px;
}}

/* ==================== 工具提示 ==================== */
QToolTip {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: 5px 9px;
    font-size: 12px;
}}

/* ==================== 滚动区与细滚动条 ==================== */
QScrollArea {{
    background-color: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 4px 1px 4px 1px;
}}
QScrollBar::handle:vertical {{
    background: rgba(255, 255, 255, 0.14);
    border-radius: 3px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(255, 255, 255, 0.26);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0px;
    background: none;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 1px 4px 1px 4px;
}}
QScrollBar::handle:horizontal {{
    background: rgba(255, 255, 255, 0.14);
    border-radius: 3px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background: rgba(255, 255, 255, 0.26);
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    width: 0px;
    background: none;
}}

/* ==================== 侧边栏与设置页结构 ==================== */
QFrame#sidebar {{
    background-color: {BG_SIDEBAR};
    border: none;
    border-right: 1px solid {BORDER_SUBTLE};
}}
QFrame#bottomBar {{
    background-color: {BG_SIDEBAR};
    border: none;
    border-top: 1px solid {BORDER_SUBTLE};
}}
QLabel#appTitle {{
    font-size: 15px;
    font-weight: 700;
    color: {TEXT_PRIMARY};
    letter-spacing: 0.2px;
}}
QLabel#appSubtitle {{
    font-size: 11px;
    color: {TEXT_MUTED};
}}
QLabel#appLogo {{
    background: transparent;
}}
QLabel#pageTitle {{
    font-size: 18px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QLabel#pageSubtitle {{
    font-size: 12.5px;
    color: {TEXT_SECONDARY};
}}
QLabel#sectionTitle {{
    font-size: 11.5px;
    font-weight: 600;
    color: {TEXT_MUTED};
    letter-spacing: 0.4px;
    padding-left: 2px;
}}
QLabel#rowTitle {{
    font-size: 13px;
    font-weight: 500;
    color: {TEXT_PRIMARY};
}}
QLabel#rowDescription {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
}}
QLabel#hintLabel {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
}}
QLabel#bannerHint {{
    background-color: {ACCENT_SOFT};
    color: #D4CEFF;
    border: 1px solid {ACCENT_BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 8px 12px;
    font-size: 12.5px;
}}
QFrame#headerDivider {{
    background-color: {BORDER_SUBTLE};
    border: none;
    max-height: 1px;
}}

/* ---- 侧边栏导航按钮 ---- */
QPushButton#navBtn {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border: 1px solid transparent;
    border-radius: {RADIUS_MD}px;
    padding: 9px 12px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#navBtn:hover {{
    background-color: rgba(255, 255, 255, 0.05);
    color: {TEXT_PRIMARY};
}}
QPushButton#navBtn:checked {{
    background-color: {ACCENT_SOFT};
    color: #FFFFFF;
    border: 1px solid rgba(124, 108, 246, 0.30);
    font-weight: 600;
}}

/* ---- SettingCard 与行分割线 ---- */
QFrame#settingCard {{
    background-color: {BG_SURFACE};
    border: 1px solid {BORDER};
    border-radius: 11px;
}}
QFrame#settingCard QWidget#settingRow {{
    background: transparent;
}}
QFrame#settingDivider {{
    background-color: {BORDER_SUBTLE};
    border: none;
    min-height: 1px;
    max-height: 1px;
}}

/* ---- 状态徽章 ---- */
QLabel#statusBadge {{
    background-color: {BG_ELEVATED};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 3px 9px;
    font-size: 12px;
    font-weight: 500;
}}
QLabel#statusBadge[tone="success"] {{
    background-color: {SUCCESS_SOFT};
    color: #86EFAC;
    border-color: {SUCCESS_BORDER};
}}
QLabel#statusBadge[tone="warning"] {{
    background-color: {WARNING_SOFT};
    color: #FCD34D;
    border-color: {WARNING_BORDER};
}}
QLabel#statusBadge[tone="danger"] {{
    background-color: {DANGER_SOFT};
    color: #FCA5A5;
    border-color: {DANGER_BORDER};
}}

/* ==================== 按钮层级 ==================== */
/* 默认 Secondary 按钮 */
QPushButton {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 6px 14px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed {{
    background-color: {BG_ACTIVE};
}}
QPushButton:disabled {{
    background-color: {BG_SURFACE};
    color: {TEXT_DISABLED};
    border-color: {BORDER_SUBTLE};
}}

/* Primary 按钮（主操作） */
QPushButton[variant="primary"] {{
    background-color: {ACCENT};
    color: #FFFFFF;
    border: 1px solid {ACCENT};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{
    background-color: {ACCENT_LIGHT};
    border-color: {ACCENT_LIGHT};
}}
QPushButton[variant="primary"]:pressed {{
    background-color: {ACCENT_DARK};
    border-color: {ACCENT_DARK};
}}
QPushButton[variant="primary"]:disabled {{
    background-color: rgba(124, 108, 246, 0.30);
    color: rgba(255, 255, 255, 0.45);
    border-color: transparent;
}}

/* Ghost 按钮（低强调次要操作） */
QPushButton[variant="ghost"] {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border: 1px solid transparent;
}}
QPushButton[variant="ghost"]:hover {{
    background-color: rgba(255, 255, 255, 0.06);
    color: {TEXT_PRIMARY};
}}
QPushButton[variant="ghost"]:pressed {{
    background-color: rgba(255, 255, 255, 0.10);
}}

/* Danger 按钮（删除 / 清除数据） */
QPushButton[variant="danger"] {{
    background-color: {DANGER_SOFT};
    color: {DANGER_TEXT};
    border: 1px solid {DANGER_BORDER};
}}
QPushButton[variant="danger"]:hover {{
    background-color: {DANGER_HOVER};
    border-color: {DANGER};
    color: #FCA5A5;
}}
QPushButton[variant="danger"]:pressed {{
    background-color: rgba(239, 68, 68, 0.30);
}}
QPushButton[variant="danger"]:disabled {{
    background-color: rgba(255, 255, 255, 0.03);
    color: #6E6E7B;
    border-color: rgba(255, 255, 255, 0.08);
}}

/* Icon-only 按钮（如 HotkeyEdit 清除按钮） */
QPushButton[variant="icon"] {{
    background-color: {BG_INPUT};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 5px;
}}
QPushButton[variant="icon"]:hover {{
    background-color: {BG_HOVER};
    color: {TEXT_PRIMARY};
    border-color: {BORDER_STRONG};
}}
QPushButton[variant="icon"]:pressed {{
    background-color: {BG_ACTIVE};
}}

/* ==================== 输入控件（LineEdit / ComboBox / SpinBox / KeySequenceEdit） ==================== */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QKeySequenceEdit {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 6px 10px;
    min-height: 20px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QKeySequenceEdit:hover {{
    border-color: {BORDER_STRONG};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QKeySequenceEdit:focus {{
    border: 1px solid {ACCENT};
    background-color: #1A1A22;
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QKeySequenceEdit:disabled {{
    background-color: {BG_RECESSED};
    color: {TEXT_DISABLED};
    border-color: {BORDER_SUBTLE};
}}

/* QComboBox 下拉箭头与弹出菜单 */
QComboBox {{
    padding-right: 28px;
}}
QComboBox:editable {{
    padding: 0px 28px 0px 0px;
    min-height: 32px;
}}
QComboBox QLineEdit {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    padding: 5px 6px 5px 10px;
    min-height: 22px;
}}
QComboBox QLineEdit:hover, QComboBox QLineEdit:focus {{
    border: none;
    background-color: transparent;
}}
QComboBox QLineEdit:disabled {{
    background-color: transparent;
    border: none;
    color: {TEXT_DISABLED};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    {down_url}
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_ELEVATED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 4px;
    outline: none;
    selection-background-color: {ACCENT_SOFT};
    selection-color: #FFFFFF;
}}
QComboBox QAbstractItemView::item {{
    min-height: 28px;
    padding: 4px 10px;
    border-radius: {RADIUS_SM}px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: rgba(255, 255, 255, 0.06);
}}
QComboBox QAbstractItemView::item:selected {{
    background-color: {ACCENT_SOFT};
    color: #FFFFFF;
}}

/* QSpinBox / QDoubleSpinBox 步进箭头 */
QSpinBox, QDoubleSpinBox {{
    padding-right: 24px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 22px;
    border: none;
    border-top-right-radius: {RADIUS_MD}px;
    background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 22px;
    border: none;
    border-bottom-right-radius: {RADIUS_MD}px;
    background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: rgba(255, 255, 255, 0.07);
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    {up_url}
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    {down_url}
    width: 10px;
    height: 10px;
}}

/* ==================== 普通复选框（修复选中清晰对勾） ==================== */
QCheckBox {{
    color: {TEXT_PRIMARY};
    spacing: 8px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border: 1px solid {BORDER_STRONG};
    border-radius: 5px;
    background-color: {BG_INPUT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT_LIGHT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    {check_url}
}}
QCheckBox::indicator:disabled {{
    background-color: {BG_RECESSED};
    border-color: {BORDER_SUBTLE};
}}

/* ==================== 进度条 ==================== */
QProgressBar {{
    background-color: {BG_RECESSED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    min-height: 10px;
    max-height: 14px;
    text-align: center;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

/* ==================== 分段控件容器与分段按钮 ==================== */
QFrame#segmentedContainer {{
    background-color: {BG_RECESSED};
    border: 1px solid {BORDER};
    border-radius: 9px;
}}
QPushButton#segmentBtn {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 16px;
    font-weight: 500;
}}
QPushButton#segmentBtn:hover {{
    color: {TEXT_PRIMARY};
    background-color: rgba(255, 255, 255, 0.05);
}}
QPushButton#segmentBtn[segmentBtn="selected"] {{
    background-color: {ACCENT};
    color: #FFFFFF;
    border-color: {ACCENT};
    font-weight: 600;
}}
QPushButton#segmentBtn:hover[segmentBtn="selected"],
QPushButton#segmentBtn:pressed[segmentBtn="selected"] {{
    background-color: {ACCENT_LIGHT};
    border-color: {ACCENT_LIGHT};
}}

/* ==================== 兼容保留：传统 QTabWidget 兜底美化 ==================== */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    background-color: {BG_WINDOW};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {BG_SURFACE};
    color: {TEXT_SECONDARY};
    padding: 7px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {BG_WINDOW};
    color: {TEXT_PRIMARY};
    border-top: 2px solid {ACCENT};
}}
"""
