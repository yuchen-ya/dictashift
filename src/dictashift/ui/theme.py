"""集中式深色 QSS 主题：调色板常量 + QSS 生成函数。

设计原则：
- 与 overlay/result_popup 既有暗色卡片风格统一（rgba(30,30,30,…) 底、#f0f0f0 文字），
  紫色 accent 沿用 overlay 状态色的 #8b5cf6。
- overlay/result_popup 的卡片配色常量也集中在此（CARD_* / POPUP_*），
  两个浮动窗只复用常量、不改布局与交互。
- settings_window 通过 setStyleSheet(app_stylesheet()) 应用整体主题；
  个别特殊样式（如「清除全部数据」危险按钮）仍保留局部内联样式。

线程模型：纯常量与字符串拼接，无线程约束。
"""

from __future__ import annotations

# ==================== 调色板 ====================

# 窗口/控件底色（由深到浅）。
BG_WINDOW = "#1e1e1e"      # 窗口底色（与卡片 rgb(30,30,30) 一致）
BG_SURFACE = "#26262c"     # 次级表面（tab 未选中态、下拉面板）
BG_INPUT = "#2b2b31"       # 输入框/按钮底
BG_HOVER = "#34343c"       # hover 提亮

BORDER = "#3f3f46"         # 常规描边
TEXT_PRIMARY = "#f0f0f0"   # 主文字（沿用卡片文字色）
TEXT_SECONDARY = "#9ca3af" # 次级文字（沿用 IDLE 状态灰）
TEXT_DISABLED = "#6b7280"  # 禁用文字（沿用 DISABLED 状态灰）

# 紫色 accent（沿用 overlay TRANSLATING 状态色）。
ACCENT = "#8b5cf6"
ACCENT_LIGHT = "#a78bfa"   # hover/高亮
ACCENT_DARK = "#7c3aed"    # pressed

# 浮动窗卡片配色（overlay/result_popup 复用；值与历史内联样式保持一致）。
CARD_BG = "rgba(30, 30, 30, 200)"
CARD_BG_POPUP = "rgba(30, 30, 30, 215)"
CARD_TEXT = "#f0f0f0"
POPUP_TEXT_BG = "rgba(255, 255, 255, 235)"
POPUP_TEXT_DARK = "#1f2937"
POPUP_BTN_BG = "rgba(255, 255, 255, 25)"
POPUP_BTN_BORDER = "rgba(255, 255, 255, 70)"
POPUP_BTN_HOVER = "rgba(255, 255, 255, 55)"
POPUP_BTN_PRESSED = "rgba(255, 255, 255, 80)"


def app_stylesheet() -> str:
    """生成设置窗口用整体深色 QSS。

    覆盖：QTabWidget 选中态、QPushButton hover/pressed/disabled、
    QLineEdit/QComboBox/SpinBox 焦点描边（紫色 accent）、QCheckBox、
    QProgressBar，以及 header 标题/分隔线样式。
    """
    return f"""
/* ---- 整体底色与文字 ---- */
QWidget {{
    background-color: {BG_WINDOW};
    color: {TEXT_PRIMARY};
}}

/* ---- 顶部 header：标题文字 + 分隔线 ---- */
QLabel#appTitle {{
    font-size: 17px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
}}
QLabel#appLogo {{
    background: transparent;
}}
QFrame#headerDivider {{
    background-color: {BORDER};
    border: none;
    max-height: 1px;
}}

/* ---- Tab：选中态白字 + 顶部紫色描边 ---- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    top: -1px;
}}
QTabBar::tab {{
    background-color: {BG_SURFACE};
    color: {TEXT_SECONDARY};
    padding: 6px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background-color: {BG_WINDOW};
    color: {TEXT_PRIMARY};
    border-top: 2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {TEXT_PRIMARY};
    background-color: {BG_HOVER};
}}

/* ---- 按钮：hover 描边提亮 / pressed 按下 / disabled 置灰 ---- */
QPushButton {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT_DARK};
    border-color: {ACCENT_DARK};
}}
QPushButton:disabled {{
    background-color: {BG_SURFACE};
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}

/* ---- 输入控件：焦点紫色描边 ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    background-color: {BG_SURFACE};
    color: {TEXT_DISABLED};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

/* ---- 复选框：选中实心紫色方块 ---- */
QCheckBox {{
    color: {TEXT_PRIMARY};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background-color: {BG_INPUT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

/* ---- 进度条（模型下载）：紫色 chunk ---- */
QProgressBar {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

/* ---- 工具提示 ---- */
QToolTip {{
    background-color: {BG_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}

/* ---- 分段按钮组（语气改写 关闭/毒舌/傲娇）----
   深色主题下 QRadioButton 选中态不明显，改用 checkable QPushButton + QButtonGroup。
   选中按钮用 accent 紫填充 + 白字；未选中为普通暗色按钮；相邻按钮无缝拼接（首圆左角、末圆右角）。 */
QPushButton#segmentBtn {{
    background-color: {BG_INPUT};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER};
    padding: 6px 18px;
}}
QPushButton#segmentBtn:first {{
    border-top-left-radius: 6px;
    border-bottom-left-radius: 6px;
}}
QPushButton#segmentBtn:last {{
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}}
QPushButton#segmentBtn[segmentBtn="selected"] {{
    background-color: {ACCENT};
    color: {TEXT_PRIMARY};
    border-color: {ACCENT};
}}
QPushButton#segmentBtn:hover {{
    border-color: {ACCENT_LIGHT};
}}
QPushButton#segmentBtn:hover[segmentBtn="selected"], QPushButton#segmentBtn:pressed[segmentBtn="selected"] {{
    background-color: {ACCENT_DARK};
    border-color: {ACCENT_DARK};
}}
"""
