"""轻量自研 Shadcn / Raycast 风格自定义 UI 组件库。

包含：
- ``ToggleSwitch(QCheckBox)``：继承 QCheckBox，完全兼容 isChecked/setChecked/toggled，
  自绘平滑抗锯齿圆角滑动开关。
- ``SettingRow(QWidget)``：标准设置行（左侧标题+可选副标题描述，右侧操作控件）。
- ``SettingCard(QFrame)``：深色圆角分组卡片，自动在子行间插入极细分割线。
- ``SettingSection(QWidget)``：可选分组标题 + ``SettingCard`` 的组合包装。
- ``SegmentedControl(QFrame)``：凹槽底托 + 无缝圆角分段按钮组，兼容 ``segmentBtn`` QSS 属性。
- ``apply_window_chrome(widget)``：基于 pywinstyles（CC0-1.0）在 Windows 真实顶层窗口上启用
  深色标题栏与自定义标题栏底色，离屏/非 Windows 环境优雅零报错降级。

线程模型：纯 UI 主线程使用。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import ACCENT, ACCENT_LIGHT, BG_WINDOW, TEXT_DISABLED, TEXT_PRIMARY

logger = logging.getLogger(__name__)


# ==================== ToggleSwitch ====================


class ToggleSwitch(QCheckBox):
    """现代圆角滑动开关（继承 QCheckBox，保持信号与属性 100% 兼容）。

    - 未设 text 时仅渲染 38×22 滑动开关，适合放在 ``SettingRow`` 右侧。
    - 设置了 text 时在开关右侧渲染文字标签。
    - 交互时使用 140ms OutCubic 平滑过渡；窗口不可见（含 offscreen 构造阶段）
      立即同步滑块位置，不残留半动画状态。
    """

    _TRACK_W = 38
    _TRACK_H = 22
    _KNOB_MARGIN = 3
    _TEXT_GAP = 8

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb_pos: float = 1.0 if self.isChecked() else 0.0
        self._hovered: bool = False

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)

        self.toggled.connect(self._on_toggled)

    def _on_anim_value(self, value: object) -> None:
        self._thumb_pos = float(value)
        self.update()

    def _on_toggled(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        if not self.isVisible():
            self._anim.stop()
            self._thumb_pos = target
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(self._thumb_pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def sizeHint(self) -> QSize:  # noqa: N802
        text = self.text()
        if not text:
            return QSize(self._TRACK_W, self._TRACK_H)
        fm = self.fontMetrics()
        w = self._TRACK_W + self._TEXT_GAP + fm.horizontalAdvance(text) + 4
        h = max(self._TRACK_H, fm.height() + 4)
        return QSize(w, h)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        enabled = self.isEnabled()
        t = self._thumb_pos
        track_y = (self.height() - self._TRACK_H) / 2.0
        track_rect = QRectF(0.5, track_y + 0.5, self._TRACK_W - 1.0, self._TRACK_H - 1.0)
        radius = (self._TRACK_H - 1.0) / 2.0

        # 计算轨道颜色（未选暗灰 <-> 选中紫 accent 插值）
        off_color = QColor("#2B2B35" if not self._hovered else "#353542")
        on_color = QColor(ACCENT_LIGHT if self._hovered else ACCENT)
        if not enabled:
            off_color = QColor("#1D1D24")
            on_color = QColor("#4A4284")

        track_color = QColor(
            round(off_color.red() + (on_color.red() - off_color.red()) * t),
            round(off_color.green() + (on_color.green() - off_color.green()) * t),
            round(off_color.blue() + (on_color.blue() - off_color.blue()) * t),
        )

        # 绘制轨道 + 细腻描边
        border_alpha = int(34 * (1.0 - t) + 18 * t) if enabled else 14
        painter.setPen(QPen(QColor(255, 255, 255, border_alpha), 1.0))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_rect, radius, radius)

        # 绘制滑块圆钮
        knob_d = self._TRACK_H - self._KNOB_MARGIN * 2
        travel = self._TRACK_W - self._KNOB_MARGIN * 2 - knob_d
        knob_x = self._KNOB_MARGIN + travel * t
        knob_y = track_y + self._KNOB_MARGIN
        knob_rect = QRectF(knob_x, knob_y, knob_d, knob_d)

        # 轻微底部阴影增加立体层次
        if enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 65))
            painter.drawEllipse(knob_rect.translated(QPointF(0.0, 0.8)))

        knob_color = QColor("#FFFFFF" if enabled else "#7A7A86")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(knob_color)
        painter.drawEllipse(knob_rect)

        # 若设置了标签文字，画在右侧
        text = self.text()
        if text:
            painter.setPen(QColor(TEXT_PRIMARY if enabled else TEXT_DISABLED))
            text_x = self._TRACK_W + self._TEXT_GAP
            text_rect = QRectF(text_x, 0, max(0.0, self.width() - text_x), self.height())
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )

        painter.end()


# ==================== SettingRow & SettingCard ====================


class SettingRow(QWidget):
    """设置卡片内的单行项：左侧标题 + 可选副标题，右侧控件（或底部堆叠控件）。"""

    def __init__(
        self,
        title: str,
        control: QWidget | None = None,
        *,
        description: str | None = None,
        stacked: bool = False,
        control_min_width: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingRow")

        self._title_label = QLabel(title, self)
        self._title_label.setObjectName("rowTitle")
        self._title_label.setWordWrap(True)

        self._desc_label: QLabel | None = None
        if description:
            self._desc_label = QLabel(description, self)
            self._desc_label.setObjectName("rowDescription")
            self._desc_label.setWordWrap(True)

        if stacked:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(16, 12, 16, 12)
            outer.setSpacing(8)
            header_box = QVBoxLayout()
            header_box.setContentsMargins(0, 0, 0, 0)
            header_box.setSpacing(2)
            header_box.addWidget(self._title_label)
            if self._desc_label is not None:
                header_box.addWidget(self._desc_label)
            outer.addLayout(header_box)
            if control is not None:
                outer.addWidget(control)
        else:
            outer = QHBoxLayout(self)
            outer.setContentsMargins(16, 12, 16, 12)
            outer.setSpacing(16)

            left = QVBoxLayout()
            left.setContentsMargins(0, 0, 0, 0)
            left.setSpacing(2)
            left.addWidget(self._title_label)
            if self._desc_label is not None:
                left.addWidget(self._desc_label)
            outer.addLayout(left, stretch=1)

            if control is not None:
                if control_min_width is not None:
                    control.setMinimumWidth(control_min_width)
                outer.addWidget(control, alignment=Qt.AlignmentFlag.AlignVCenter)

    @property
    def title_label(self) -> QLabel:
        return self._title_label

    @property
    def description_label(self) -> QLabel | None:
        return self._desc_label


class SettingCard(QFrame):
    """深色圆角卡片容器：自动在添加的行/组件间绘制极细 1px 分割线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._item_count = 0

    def _add_divider_if_needed(self) -> None:
        if self._item_count > 0:
            line = QFrame(self)
            line.setObjectName("settingDivider")
            line.setFrameShape(QFrame.Shape.HLine)
            self._layout.addWidget(line)

    def add_row(
        self,
        title: str,
        control: QWidget | None = None,
        *,
        description: str | None = None,
        stacked: bool = False,
        control_min_width: int | None = None,
    ) -> SettingRow:
        """添加标准设置行并自动插入分割线。"""
        self._add_divider_if_needed()
        row = SettingRow(
            title,
            control,
            description=description,
            stacked=stacked,
            control_min_width=control_min_width,
            parent=self,
        )
        self._layout.addWidget(row)
        self._item_count += 1
        return row

    def add_widget(self, widget: QWidget, *, margins: tuple[int, int, int, int] = (16, 12, 16, 12)) -> QWidget:
        """向卡片内添加一个自定义区块并自动插入分割线。"""
        self._add_divider_if_needed()
        wrapper = QWidget(self)
        wrapper.setObjectName("settingRow")
        lay = QVBoxLayout(wrapper)
        lay.setContentsMargins(*margins)
        lay.setSpacing(0)
        lay.addWidget(widget)
        self._layout.addWidget(wrapper)
        self._item_count += 1
        return wrapper


class SettingSection(QWidget):
    """分组标题 + SettingCard 的标准设置块组合。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("transparentWrap")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._title_label = QLabel(title, self)
        self._title_label.setObjectName("sectionTitle")
        lay.addWidget(self._title_label)

        self.card = SettingCard(self)
        lay.addWidget(self.card)

    def add_row(self, *args, **kwargs) -> SettingRow:
        return self.card.add_row(*args, **kwargs)

    def add_widget(self, *args, **kwargs) -> QWidget:
        return self.card.add_widget(*args, **kwargs)


# ==================== SegmentedControl ====================


class SegmentedControl(QFrame):
    """现代凹槽分段选择控件（checkable QPushButton + QButtonGroup）。

    特点：
    - 保持每个按钮 ``objectName="segmentBtn"`` 与 ``segmentBtn="selected"`` 动态属性，
      既呈现现代胶囊分段高亮，也与既有 QSS/测试完全兼容。
    - 发出 ``selection_changed(str)`` 信号携带选中项 key。
    """

    selection_changed = Signal(str)

    def __init__(
        self,
        options: Sequence[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        """
        Args:
            options: ``[(key, label), ...]`` 列表，例如 ``[("off", "关闭"), ("toxic", "毒舌")]``。
        """
        super().__init__(parent)
        self.setObjectName("segmentedContainer")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        self._key_by_btn: dict[QPushButton, str] = {}

        for key, label in options:
            btn = QPushButton(label, self)
            btn.setObjectName("segmentBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.toggled.connect(self._on_button_toggled)
            self._group.addButton(btn)
            layout.addWidget(btn)
            self._buttons[key] = btn
            self._key_by_btn[btn] = key

        if options:
            first_key = options[0][0]
            self._buttons[first_key].setChecked(True)
        self.refresh_styles()

    @property
    def button_group(self) -> QButtonGroup:
        return self._group

    def button(self, key: str) -> QPushButton:
        return self._buttons[key]

    def current_key(self) -> str:
        for key, btn in self._buttons.items():
            if btn.isChecked():
                return key
        return next(iter(self._buttons.keys()), "")

    def set_current_key(self, key: str) -> None:
        btn = self._buttons.get(key)
        if btn is None and self._buttons:
            btn = next(iter(self._buttons.values()))
        if btn is not None:
            btn.setChecked(True)
        self.refresh_styles()

    def refresh_styles(self) -> None:
        for btn in self._buttons.values():
            btn.setProperty("segmentBtn", "selected" if btn.isChecked() else "")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _on_button_toggled(self, checked: bool) -> None:
        self.refresh_styles()
        if checked:
            sender = self.sender()
            if isinstance(sender, QPushButton) and sender in self._key_by_btn:
                self.selection_changed.emit(self._key_by_btn[sender])


# ==================== Windows 原生深色标题栏 ====================


def apply_window_chrome(
    widget: QWidget,
    *,
    header_color: str = BG_WINDOW,
    title_color: str = TEXT_PRIMARY,
) -> bool:
    """在 Windows 真实顶层窗口上启用深色标题栏与协调的标题栏颜色（基于 pywinstyles）。

    - 非 Windows、offscreen/minimal 平台、非顶层窗口或 DWM 不支持时静默降级返回 False，
      绝不抛出异常。
    """
    if sys.platform != "win32":
        return False
    if widget is None or not widget.isWindow():
        return False
    try:
        platform = QGuiApplication.platformName().lower()
        if platform in ("offscreen", "minimal", ""):
            return False
        import pywinstyles

        pywinstyles.apply_style(widget, "dark")
        try:
            pywinstyles.change_header_color(widget, header_color)
            pywinstyles.change_title_color(widget, title_color)
        except Exception:  # noqa: BLE001 - Win10 不支持自定义标题栏色，dark 仍已生效
            pass
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[widgets] apply_window_chrome 跳过或降级", exc_info=True)
        return False
