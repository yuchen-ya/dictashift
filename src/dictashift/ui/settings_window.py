"""设置窗口（现代左侧导航 + 分组卡片滚动布局）。

页面结构（保持 5 个完整配置页）：
1. 常规：启用语音输入、开机启动（读写 Windows 注册表）、全局禁用快捷键、
   结果展示方式（右下角弹窗/常驻悬浮窗 + 测试弹窗）、恢复默认设置、
   清除全部数据并退出。
2. 快捷键：中文输入(f8)、中文→英文(f9)、取消当前任务(esc)、快速查词(alt+q)、划词翻译开关(f7)。
3. 语音识别：模型状态（已安装/未安装 + 下载/重新下载 + 进度条）、实际运行设备、
   硬件重检、麦克风、即按即录、最长录音秒数。
4. 翻译：当前配置（内置免登录 Google/Bing + 用户 LLM profiles）、编辑区
   （名称/Base URL/模型下拉与自动获取/API Key）、保存为新配置、测试连接；
   划词翻译分组（总开关/自动弹出/弹窗显示时长）。
5. 个性化：句尾后缀（开关 + 文本）、语气改写（关闭/毒舌/傲娇分段选择，
   复用翻译页活动档案的 LLM 配置）。

线程模型：
- 控件值 <-> AppSettings 双向绑定（load_from_settings / apply_to_settings）。
- 重后台工作（ASR initialize、翻译测试连接、模型下载）通过 Qt signal 交给
  application 层在后台线程执行，结果再 queued 回 UI；UI 线程不做网络/模型工作。
"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from importlib import metadata as _importlib_metadata

from ..asr.providers import AsrDevice
from ..audio.devices import list_input_devices
from ..resources import app_icon_png_path
from ..settings import AppSettings
from ..settings.launch_at_login import (
    is_launch_at_login_enabled,
    set_launch_at_login,
)
from ..settings.models import (
    AsrSettings,
    HotkeySettings,
    PersonalizeSettings,
    SelectionTranslateSettings,
    UiSettings,
)
from ..translation import TranslationProfile, builtin_free_profiles, builtin_profile_names
from .hotkey_editor import HotkeyEdit
from .icons import get_icon
from .theme import DANGER_TEXT, TEXT_PRIMARY, TEXT_SECONDARY, app_stylesheet
from .widgets import (
    SegmentedControl,
    SettingSection,
    ToggleSwitch,
    apply_window_chrome,
)

# 内置免登录翻译项顺序（与「当前翻译配置」ComboBox 内置段顺序一致）。
_BUILTIN_PROFILES = list(builtin_free_profiles())
_BUILTIN_NAMES = builtin_profile_names()

try:
    _APP_VERSION = _importlib_metadata.version("dictashift")
except Exception:  # noqa: BLE001
    _APP_VERSION = "0.8.0"

# 侧边栏导航定义：(标题, 图标语义名, 页面副标题)
_NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("常规", "general", "管理应用开关、开机启动、结果展示方式与数据维护"),
    ("快捷键", "hotkeys", "自定义语音听写、快速查词、取消任务与划词翻译全局热键"),
    ("语音识别", "asr", "管理离线语音识别模型、推理设备与麦克风采集参数"),
    ("翻译", "translation", "配置翻译服务引擎、自定义大模型档案与划词翻译"),
    ("个性化", "personalize", "定制输出句尾后缀与大模型语气改写风格"),
)


class _NavTabsAdapter:
    """轻量适配层：将侧边栏 + QStackedWidget 适配为 QTabWidget 常用接口。

    兼容既有测试与截图脚本对 ``win._tabs.count()`` / ``tabText(i)`` /
    ``setCurrentIndex(i)`` / ``currentIndex()`` 的访问。
    """

    def __init__(self, window: "SettingsWindow") -> None:
        self._win = window

    def count(self) -> int:
        return self._win._stack.count()

    def tabText(self, index: int) -> str:  # noqa: N802
        if 0 <= index < len(self._win._nav_titles):
            return self._win._nav_titles[index]
        return ""

    def currentIndex(self) -> int:  # noqa: N802
        return self._win._stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        self._win._select_page(index)

    def widget(self, index: int) -> QWidget | None:
        return self._win._stack.widget(index)


class SettingsWindow(QWidget):
    """现代侧边栏分组卡片设置窗口。

    信号（application 层连接）：
    - applied(settings)：用户点击「应用」或「确定」时发出，携带最新设置副本。
    - recheck_hardware_requested()：用户点击「重新检测硬件」（无参，固定重新初始化）。
    - test_connection_requested(profile)：用户点击「测试连接」（后台执行）。
    - fetch_models_requested(base_url, api_key, silent)：手动或自动拉取可用模型列表（后台执行）。
    - model_download_requested()：用户点击「下载模型」（后台执行，无参）。
    - test_popup_requested()：用户点击「测试右下角弹窗」。
    - clear_data_requested()：用户确认「清除全部数据并退出」后发出。
    """

    applied = Signal(AppSettings)
    recheck_hardware_requested = Signal()
    test_connection_requested = Signal(object)  # TranslationProfile
    fetch_models_requested = Signal(str, str, bool)  # base_url, api_key, silent
    model_download_requested = Signal()
    test_popup_requested = Signal()
    clear_data_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DictaShift 设置")
        self.resize(760, 580)
        self.setMinimumSize(680, 500)

        # 最近一次 load_from_settings 缓存的 settings 副本，供 ok/apply 作为基底。
        self._last_loaded: AppSettings = AppSettings()
        self._chrome_applied: bool = False

        # 导航状态记录
        self._nav_titles: list[str] = []
        self._nav_subtitles: list[str] = []
        self._nav_icon_names: list[str] = []
        self._nav_buttons: list[QPushButton] = []
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        # ---- 顶层左右两栏布局 ----
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = self._build_sidebar()
        root.addWidget(sidebar)

        # 右侧主体：页面标题区 + QStackedWidget（滚动页）+ 固定底栏
        right_pane = QWidget(self)
        right_layout = QVBoxLayout(right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        page_header = QWidget(right_pane)
        ph_layout = QVBoxLayout(page_header)
        ph_layout.setContentsMargins(24, 20, 24, 14)
        ph_layout.setSpacing(4)
        self._page_title_label = QLabel("", page_header)
        self._page_title_label.setObjectName("pageTitle")
        self._page_subtitle_label = QLabel("", page_header)
        self._page_subtitle_label.setObjectName("pageSubtitle")
        ph_layout.addWidget(self._page_title_label)
        ph_layout.addWidget(self._page_subtitle_label)
        right_layout.addWidget(page_header)

        header_divider = QFrame(right_pane)
        header_divider.setObjectName("headerDivider")
        header_divider.setFrameShape(QFrame.Shape.HLine)
        right_layout.addWidget(header_divider)

        self._stack = QStackedWidget(right_pane)
        pages = [
            self._build_general_tab(),
            self._build_hotkeys_tab(),
            self._build_asr_tab(),
            self._build_translation_tab(),
            self._build_personalize_tab(),
        ]
        for idx, (title, icon_name, subtitle) in enumerate(_NAV_ITEMS):
            self._add_nav_page(idx, title, icon_name, subtitle, pages[idx])

        right_layout.addWidget(self._stack, stretch=1)
        right_layout.addWidget(self._build_bottom_bar())
        root.addWidget(right_pane, stretch=1)

        # 兼容适配：保持 win._tabs.count()/tabText()/setCurrentIndex() 可用。
        self._tabs = _NavTabsAdapter(self)
        self._select_page(0)

        # 保证独立实例化 SettingsWindow（测试/脚本）也拥有完整现代暗色样式。
        app = QApplication.instance()
        if app is None or not app.styleSheet():
            self.setStyleSheet(app_stylesheet())

        self._ok_btn.clicked.connect(self._on_ok)
        self._apply_btn.clicked.connect(self._on_apply)
        self._cancel_btn.clicked.connect(self.hide)

    def _build_sidebar(self) -> QFrame:
        """构建左侧导航栏：顶部品牌区 + 垂直导航项 + 底部版本标识。"""
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)

        lay = QVBoxLayout(sidebar)
        lay.setContentsMargins(12, 18, 12, 16)
        lay.setSpacing(6)

        # 品牌头部
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(6, 0, 6, 12)
        brand_row.setSpacing(10)

        self._logo_label = QLabel(sidebar)
        self._logo_label.setObjectName("appLogo")
        self._logo_label.setFixedSize(28, 28)
        logo_pm = QPixmap(str(app_icon_png_path()))
        if logo_pm.isNull():
            self._logo_label.hide()
        else:
            self._logo_label.setPixmap(
                logo_pm.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )
        brand_row.addWidget(self._logo_label)

        brand_text = QVBoxLayout()
        brand_text.setContentsMargins(0, 0, 0, 0)
        brand_text.setSpacing(1)
        title_label = QLabel("DictaShift", sidebar)
        title_label.setObjectName("appTitle")
        subtitle_label = QLabel("语音听写与翻译", sidebar)
        subtitle_label.setObjectName("appSubtitle")
        brand_text.addWidget(title_label)
        brand_text.addWidget(subtitle_label)
        brand_row.addLayout(brand_text, stretch=1)
        lay.addLayout(brand_row)

        # 导航项容器（由 _add_nav_page 填充）
        self._nav_layout = QVBoxLayout()
        self._nav_layout.setContentsMargins(0, 4, 0, 0)
        self._nav_layout.setSpacing(4)
        lay.addLayout(self._nav_layout)

        lay.addStretch(1)

        ver_label = QLabel(f"v{_APP_VERSION}", sidebar)
        ver_label.setObjectName("appSubtitle")
        ver_label.setContentsMargins(8, 0, 8, 0)
        lay.addWidget(ver_label)

        return sidebar

    def _build_bottom_bar(self) -> QFrame:
        """底部操作栏：取消 / 应用 / 确定。"""
        bar = QFrame(self)
        bar.setObjectName("bottomBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(8)

        lay.addStretch(1)
        self._cancel_btn = QPushButton("取消", bar)
        self._cancel_btn.setProperty("variant", "ghost")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._apply_btn = QPushButton("应用", bar)
        self._apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._ok_btn = QPushButton("确定", bar)
        self._ok_btn.setProperty("variant", "primary")
        self._ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        lay.addWidget(self._cancel_btn)
        lay.addWidget(self._apply_btn)
        lay.addWidget(self._ok_btn)
        return bar

    def _add_nav_page(
        self,
        index: int,
        title: str,
        icon_name: str,
        subtitle: str,
        page_widget: QWidget,
    ) -> None:
        self._nav_titles.append(title)
        self._nav_subtitles.append(subtitle)
        self._nav_icon_names.append(icon_name)

        btn = QPushButton(f"  {title}", self)
        btn.setObjectName("navBtn")
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setIconSize(QSize(16, 16))
        btn.clicked.connect(lambda _checked=False, i=index: self._select_page(i))
        self._nav_group.addButton(btn, index)
        self._nav_buttons.append(btn)
        self._nav_layout.addWidget(btn)

        self._stack.addWidget(self._wrap_scroll_page(page_widget))

    def _select_page(self, index: int) -> None:
        if not (0 <= index < self._stack.count()):
            return
        self._stack.setCurrentIndex(index)
        self._page_title_label.setText(self._nav_titles[index])
        self._page_subtitle_label.setText(self._nav_subtitles[index])
        for i, btn in enumerate(self._nav_buttons):
            active = i == index
            btn.setChecked(active)
            icon = get_icon(
                self._nav_icon_names[i],
                color="#FFFFFF" if active else TEXT_SECONDARY,
                color_active="#FFFFFF",
            )
            if not icon.isNull():
                btn.setIcon(icon)

    @staticmethod
    def _wrap_scroll_page(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    @staticmethod
    def _set_btn_icon(
        btn: QPushButton,
        icon_name: str,
        color: str = TEXT_PRIMARY,
        size: int = 14,
        *,
        color_disabled: str | None = None,
    ) -> None:
        icon = get_icon(icon_name, color=color, color_disabled=color_disabled)
        if not icon.isNull():
            btn.setIcon(icon)
            btn.setIconSize(QSize(size, size))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """窗口显示时启用深色原生标题栏并刷新一次模型状态。"""
        super().showEvent(event)
        if not self._chrome_applied:
            self._chrome_applied = apply_window_chrome(self)
        try:
            self.refresh_model_status()
        except Exception:  # noqa: BLE001
            pass

    # ==================== 常规页 ====================

    def _build_general_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 18, 24, 24)
        outer.setSpacing(18)

        # ---- 分组 1：基本行为 ----
        basic_sec = SettingSection("基本行为", page)
        self._enable_check = ToggleSwitch("", page)
        basic_sec.add_row(
            "启用语音输入",
            self._enable_check,
            description="总开关，关闭后将暂停所有语音听写快捷键响应",
        )

        self._autostart_check = ToggleSwitch("", page)
        basic_sec.add_row(
            "开机启动",
            self._autostart_check,
            description="登录 Windows 时自动在系统托盘后台运行 DictaShift",
        )

        self._toggle_hotkey = HotkeyEdit(page)
        basic_sec.add_row(
            "全局禁用快捷键",
            self._toggle_hotkey,
            description="一键暂停或重新启用语音听写功能",
            control_min_width=210,
        )
        outer.addWidget(basic_sec)

        # ---- 分组 2：结果展示 ----
        result_sec = SettingSection("结果展示", page)
        result_ctrl = QWidget(page)
        result_ctrl.setObjectName("transparentWrap")
        result_row = QHBoxLayout(result_ctrl)
        result_row.setContentsMargins(0, 0, 0, 0)
        result_row.setSpacing(8)

        self._result_ui_combo = QComboBox(result_ctrl)
        self._result_ui_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._result_ui_combo.setMinimumContentsLength(14)
        self._result_ui_combo.addItem(
            "右下角弹窗（失败时弹出，平时无悬浮窗）", "popup"
        )
        self._result_ui_combo.addItem(
            "常驻悬浮窗（置顶显示状态与结果）", "overlay"
        )
        self._test_popup_btn = QPushButton("测试右下角弹窗", result_ctrl)
        self._test_popup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._test_popup_btn, "popup", TEXT_SECONDARY)
        self._test_popup_btn.clicked.connect(self._on_test_popup_clicked)

        result_row.addWidget(self._result_ui_combo, stretch=1)
        result_row.addWidget(self._test_popup_btn)

        result_sec.add_row(
            "结果展示方式",
            result_ctrl,
            description="当目标窗口不支持直输时，使用右下角卡片或常驻悬浮窗展示文本",
            stacked=True,
        )
        outer.addWidget(result_sec)

        # ---- 分组 3：维护与数据 ----
        maint_sec = SettingSection("维护与数据", page)

        reset_ctrl = QWidget(page)
        reset_ctrl.setObjectName("transparentWrap")
        reset_lay = QVBoxLayout(reset_ctrl)
        reset_lay.setContentsMargins(0, 0, 0, 0)
        reset_lay.setSpacing(4)
        self._reset_btn = QPushButton("恢复默认设置", reset_ctrl)
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._reset_btn, "reset", TEXT_SECONDARY)
        self._reset_btn.clicked.connect(self._on_reset_clicked)
        self._reset_status_label = QLabel("", reset_ctrl)
        self._reset_status_label.setObjectName("hintLabel")
        self._reset_status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        reset_lay.addWidget(self._reset_btn, alignment=Qt.AlignmentFlag.AlignRight)
        reset_lay.addWidget(self._reset_status_label)
        maint_sec.add_row(
            "恢复默认设置",
            reset_ctrl,
            description="重置常规、快捷键、语音识别、个性化与划词翻译，不影响翻译配置",
        )

        self._clear_data_btn = QPushButton("清除全部数据并退出…", page)
        self._clear_data_btn.setObjectName("clear_data_btn")
        self._clear_data_btn.setProperty("variant", "danger")
        self._clear_data_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._clear_data_btn, "delete", DANGER_TEXT)
        self._clear_data_btn.clicked.connect(self._on_clear_data_clicked)
        maint_sec.add_row(
            "清除全部数据",
            self._clear_data_btn,
            description="删除所有设置（含翻译 API Key）、日志与本地已下载模型并立即退出",
        )
        outer.addWidget(maint_sec)

        outer.addStretch(1)
        return page

    # ==================== 快捷键页 ====================

    def _build_hotkeys_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 18, 24, 24)
        outer.setSpacing(18)

        dictate_sec = SettingSection("语音听写与控制", page)

        self._dictate_zh_edit = HotkeyEdit(page)
        dictate_sec.add_row(
            "中文输入",
            self._dictate_zh_edit,
            description="按住开始录音，松开后直接输入识别出的中文",
            control_min_width=220,
        )

        self._dictate_en_edit = HotkeyEdit(page)
        dictate_sec.add_row(
            "中文 → 英文",
            self._dictate_en_edit,
            description="按住说中文，松开后自动翻译并输入英文",
            control_min_width=220,
        )

        self._cancel_edit = HotkeyEdit(page)
        dictate_sec.add_row(
            "取消当前任务",
            self._cancel_edit,
            description="立即中止正在进行的录音、识别或翻译流程",
            control_min_width=220,
        )
        outer.addWidget(dictate_sec)

        sel_sec = SettingSection("快速查词与划词翻译", page)
        self._quick_lookup_edit = HotkeyEdit(page)
        sel_sec.add_row(
            "快速查词",
            self._quick_lookup_edit,
            description="有选中文本时直接翻译，否则打开查词输入框；浮窗已打开时再按关闭",
            control_min_width=220,
        )

        self._selection_translate_edit = HotkeyEdit(page)
        sel_sec.add_row(
            "划词翻译开关",
            self._selection_translate_edit,
            description="单次按下以切换划词翻译监听的开启与关闭状态",
            control_min_width=220,
        )
        outer.addWidget(sel_sec)

        outer.addStretch(1)
        return page

    # ==================== 语音识别页 ====================

    def _build_asr_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 18, 24, 24)
        outer.setSpacing(18)

        # ---- 分组 1：识别模型与硬件 ----
        model_sec = SettingSection("识别模型与运行环境", page)

        model_ctrl = QWidget(page)
        model_ctrl.setObjectName("transparentWrap")
        model_col = QVBoxLayout(model_ctrl)
        model_col.setContentsMargins(0, 0, 0, 0)
        model_col.setSpacing(8)

        model_top = QHBoxLayout()
        model_top.setContentsMargins(0, 0, 0, 0)
        model_top.setSpacing(10)
        self._model_status_label = QLabel("（检测中…）", model_ctrl)
        self._model_status_label.setObjectName("statusBadge")
        self._model_status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._model_status_label.setWordWrap(False)
        self._download_model_btn = QPushButton("下载模型", model_ctrl)
        self._download_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._download_model_btn, "download", TEXT_PRIMARY)
        self._download_model_btn.clicked.connect(self._on_download_model_clicked)
        model_top.addWidget(self._model_status_label)
        model_top.addStretch(1)
        model_top.addWidget(self._download_model_btn)
        model_col.addLayout(model_top)

        self._model_progress = QProgressBar(model_ctrl)
        self._model_progress.setRange(0, 100)
        self._model_progress.hide()
        model_col.addWidget(self._model_progress)

        model_sec.add_row(
            "本地语音模型",
            model_ctrl,
            description="SenseVoice ONNX 离线语音识别模型，首次使用需下载至本机",
            stacked=True,
        )

        self._actual_device_label = QLabel("（未初始化）", page)
        self._actual_device_label.setObjectName("statusBadge")
        model_sec.add_row(
            "实际运行设备",
            self._actual_device_label,
            description="当前本地语音推理引擎所使用的计算后端",
        )

        diag_ctrl = QWidget(page)
        diag_ctrl.setObjectName("transparentWrap")
        diag_row = QHBoxLayout(diag_ctrl)
        diag_row.setContentsMargins(0, 0, 0, 0)
        diag_row.setSpacing(8)
        self._recheck_btn = QPushButton("重新检测硬件", diag_ctrl)
        self._recheck_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._recheck_btn, "refresh", TEXT_SECONDARY)
        self._recheck_btn.clicked.connect(self._on_recheck_clicked)

        self._test_asr_btn = QPushButton("测试识别", diag_ctrl)
        self._test_asr_btn.setEnabled(False)  # 第一版禁用态占位
        diag_row.addWidget(self._recheck_btn)
        diag_row.addWidget(self._test_asr_btn)
        model_sec.add_row(
            "引擎诊断",
            diag_ctrl,
            description="重新探测推理环境并重新初始化语音识别服务",
        )
        outer.addWidget(model_sec)

        # ---- 分组 2：音频采集 ----
        audio_sec = SettingSection("音频采集", page)

        self._mic_combo = QComboBox(page)
        self._mic_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._mic_combo.setMinimumContentsLength(18)
        self._mic_combo.addItem("系统默认设备")
        for dev in list_input_devices():
            self._mic_combo.addItem(
                f"{dev['name']} (#{dev['index']})", dev["index"]
            )
        audio_sec.add_row(
            "麦克风设备",
            self._mic_combo,
            description="选择听写使用的录音输入设备",
            stacked=True,
        )

        self._instant_recording_check = ToggleSwitch("", page)
        audio_sec.add_row(
            "即按即录（保持麦克风开启）",
            self._instant_recording_check,
            description="语音输入启用期间保持麦克风开启。仅在按住听写热键时保留音频，暂停或退出会关闭麦克风。",
        )

        self._max_record_spin = QSpinBox(page)
        self._max_record_spin.setRange(5, 120)
        self._max_record_spin.setSuffix(" 秒")
        self._max_record_spin.setMinimumWidth(110)
        audio_sec.add_row(
            "最长录音秒数",
            self._max_record_spin,
            description="单次按住听写的最大时长，到达上限后自动停止并识别",
        )
        outer.addWidget(audio_sec)

        outer.addStretch(1)
        return page

    # ==================== 翻译页 ====================

    def _build_translation_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 18, 24, 24)
        outer.setSpacing(18)

        # ---- 分组 1：翻译服务配置 ----
        profile_sec = SettingSection("翻译引擎与大模型配置", page)

        selector_wrap = QWidget(page)
        selector_wrap.setObjectName("transparentWrap")
        selector_row = QHBoxLayout(selector_wrap)
        selector_row.setContentsMargins(0, 0, 0, 0)
        selector_row.setSpacing(8)

        self._profile_combo = QComboBox(selector_wrap)
        self._profile_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._profile_combo.setMinimumContentsLength(14)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)

        self._new_profile_btn = QPushButton("新建", selector_wrap)
        self._new_profile_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._new_profile_btn, "add", TEXT_PRIMARY)
        self._new_profile_btn.clicked.connect(self._on_new_profile_clicked)

        self._delete_profile_btn = QPushButton("删除", selector_wrap)
        self._delete_profile_btn.setProperty("variant", "danger")
        self._delete_profile_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(
            self._delete_profile_btn, "delete", DANGER_TEXT, color_disabled="#6E6E7B"
        )
        self._delete_profile_btn.setEnabled(False)
        self._delete_profile_btn.clicked.connect(self._on_delete_profile_clicked)

        selector_row.addWidget(self._profile_combo, stretch=1)
        selector_row.addWidget(self._new_profile_btn)
        selector_row.addWidget(self._delete_profile_btn)

        profile_sec.add_row(
            "当前翻译配置",
            selector_wrap,
            description="内置 Google / Bing 免登录服务，或添加任意 OpenAI 兼容大模型",
            stacked=True,
        )

        # 编辑区区块
        edit_panel = QWidget(page)
        edit_panel.setObjectName("transparentWrap")
        edit_lay = QVBoxLayout(edit_panel)
        edit_lay.setContentsMargins(0, 0, 0, 0)
        edit_lay.setSpacing(12)

        self._builtin_hint_label = QLabel("免登录翻译服务，无需配置", edit_panel)
        self._builtin_hint_label.setObjectName("bannerHint")
        edit_lay.addWidget(self._builtin_hint_label)

        # 自定义大模型表单容器：选中内置免费服务时整体隐藏，保持页面紧凑清爽
        self._custom_form_container = QWidget(edit_panel)
        self._custom_form_container.setObjectName("transparentWrap")
        form = QFormLayout(self._custom_form_container)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._name_edit = QLineEdit(self._custom_form_container)
        self._name_edit.setPlaceholderText("例如：本地 llama-server 或 DeepSeek")
        form.addRow("配置名称", self._name_edit)

        self._base_url_edit = QLineEdit(self._custom_form_container)
        self._base_url_edit.setPlaceholderText("https://api.openai.com/v1 或 http://127.0.0.1:19120/v1")
        self._base_url_edit.editingFinished.connect(self._on_endpoint_editing_finished)
        form.addRow("Base URL", self._base_url_edit)

        model_row_wrap = QWidget(self._custom_form_container)
        model_row_wrap.setObjectName("transparentWrap")
        model_row = QHBoxLayout(model_row_wrap)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)

        self._model_combo = QComboBox(model_row_wrap)
        self._model_combo.setEditable(True)
        self._model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._model_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._model_combo.setMinimumContentsLength(14)
        self._model_edit = self._model_combo.lineEdit()
        self._model_edit.setPlaceholderText("选择或手动输入模型 ID")

        self._fetch_models_btn = QPushButton("获取模型", model_row_wrap)
        self._fetch_models_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._fetch_models_btn, "refresh", TEXT_SECONDARY)
        self._fetch_models_btn.clicked.connect(self._on_fetch_models_clicked)

        model_row.addWidget(self._model_combo, stretch=1)
        model_row.addWidget(self._fetch_models_btn)
        form.addRow("模型", model_row_wrap)

        self._api_key_edit = QLineEdit(self._custom_form_container)
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("本地模型可留空")
        self._api_key_edit.editingFinished.connect(self._on_endpoint_editing_finished)
        form.addRow("API Key", self._api_key_edit)

        edit_lay.addWidget(self._custom_form_container)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 2, 0, 0)
        action_row.setSpacing(8)
        self._save_new_btn = QPushButton("保存为新配置", edit_panel)
        self._save_new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._save_new_btn, "check", TEXT_PRIMARY)
        self._save_new_btn.clicked.connect(self._on_save_new_clicked)

        self._test_conn_btn = QPushButton("测试连接", edit_panel)
        self._test_conn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_btn_icon(self._test_conn_btn, "test", TEXT_SECONDARY)
        self._test_conn_btn.clicked.connect(self._on_test_conn_clicked)

        self._test_result_label = QLabel("", edit_panel)
        self._test_result_label.setObjectName("hintLabel")
        self._test_result_label.setWordWrap(True)

        action_row.addWidget(self._save_new_btn)
        action_row.addWidget(self._test_conn_btn)
        action_row.addWidget(self._test_result_label, stretch=1)
        edit_lay.addLayout(action_row)

        profile_sec.add_widget(edit_panel, margins=(16, 14, 16, 14))
        outer.addWidget(profile_sec)

        # ---- 分组 2：划词翻译 ----
        sel_sec = SettingSection("划词翻译", page)

        self._selection_enabled_check = ToggleSwitch("", page)
        sel_sec.add_row(
            "启用划词翻译",
            self._selection_enabled_check,
            description="开启全局鼠标划选监听（可通过快捷键一键开关）",
        )

        self._selection_auto_check = ToggleSwitch("", page)
        self._selection_enabled_check.toggled.connect(self._selection_auto_check.setEnabled)
        self._selection_auto_check.setEnabled(self._selection_enabled_check.isChecked())
        sel_sec.add_row(
            "划选文字后自动弹出翻译",
            self._selection_auto_check,
            description="鼠标划选并松开后，自动检测语言并在光标旁弹出译文卡片",
        )

        self._selection_timeout_spin = QSpinBox(page)
        self._selection_timeout_spin.setRange(5, 60)
        self._selection_timeout_spin.setSuffix(" 秒")
        self._selection_timeout_spin.setMinimumWidth(110)
        sel_sec.add_row(
            "弹窗显示时长",
            self._selection_timeout_spin,
            description="无操作后划词翻译卡片自动隐藏的时间",
        )
        outer.addWidget(sel_sec)
        outer.addStretch(1)

        # 内存中维护的用户 LLM profiles 列表（编辑/保存时操作；apply 时同步到 settings）。
        self._profiles: list[TranslationProfile] = []
        self._new_profile_draft: bool = False
        self._last_fetched_models_key: tuple[str, str] | None = None
        self._pending_fetch_key: tuple[str, str] | None = None
        self._fetching_models: bool = False

        return page

    # ==================== 个性化页 ====================

    def _build_personalize_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 18, 24, 24)
        outer.setSpacing(18)

        # ---- 分组 1：句尾后缀 ----
        suffix_sec = SettingSection("句尾后缀", page)

        self._suffix_check = ToggleSwitch("", page)
        suffix_sec.add_row(
            "启用句尾后缀",
            self._suffix_check,
            description="在最终输出文本末尾自动追加自定义后缀（本地拼接，不消耗大模型）",
        )

        self._suffix_edit = QLineEdit(page)
        self._suffix_edit.setPlaceholderText("喵")
        self._suffix_check.toggled.connect(self._suffix_edit.setEnabled)
        self._suffix_edit.setEnabled(self._suffix_check.isChecked())
        suffix_sec.add_row(
            "后缀文本",
            self._suffix_edit,
            description="例如「喵」「捏」或表情符号",
            control_min_width=200,
        )
        outer.addWidget(suffix_sec)

        # ---- 分组 2：语气改写 ----
        rewrite_sec = SettingSection("语气改写", page)

        self._rewrite_segment = SegmentedControl(
            [("off", "关闭"), ("toxic", "毒舌"), ("tsundere", "傲娇")],
            parent=page,
        )
        self._rewrite_off_btn = self._rewrite_segment.button("off")
        self._rewrite_toxic_btn = self._rewrite_segment.button("toxic")
        self._rewrite_tsundere_btn = self._rewrite_segment.button("tsundere")
        self._rewrite_group = self._rewrite_segment.button_group

        rewrite_sec.add_row(
            "改写风格",
            self._rewrite_segment,
            description=(
                "改写使用「翻译」页当前活动档案的大模型配置；"
                "活动档案需为 OpenAI 兼容型，否则改写自动跳过。"
            ),
        )
        outer.addWidget(rewrite_sec)

        outer.addStretch(1)
        return page

    def _refresh_rewrite_segment_style(self, *_args) -> None:
        """按当前选中态刷新分段按钮的 segmentBtn 属性（驱动 theme.py 定向样式）。"""
        self._rewrite_segment.refresh_styles()

    def _set_rewrite_style(self, value: str) -> None:
        """把 rewrite_style 值同步到分段按钮；非法值回退到 off。"""
        self._rewrite_segment.set_current_key(value if value in ("toxic", "tsundere") else "off")

    def _current_rewrite_style(self) -> str:
        return self._rewrite_segment.current_key()

    # ==================== 双向绑定 ====================

    def load_from_settings(self, settings: AppSettings) -> None:
        """从 AppSettings 填充所有控件值，并缓存基底副本。

        「开机启动」勾选状态从注册表实时读取（is_launch_at_login_enabled），
        不使用 settings.ui.launch_at_login（后者仅作记录）。
        """
        import dataclasses
        self._last_loaded = dataclasses.replace(settings)
        # 常规
        self._enable_check.setChecked(settings.enabled)
        try:
            self._autostart_check.setChecked(is_launch_at_login_enabled())
        except Exception:  # noqa: BLE001
            self._autostart_check.setChecked(False)
        self._set_result_ui(settings.ui.result_ui)
        self._toggle_hotkey.set_config_text(settings.hotkeys.toggle_enabled)
        self._reset_status_label.setText("")
        # 快捷键
        self._dictate_zh_edit.set_config_text(settings.hotkeys.dictate_zh)
        self._dictate_en_edit.set_config_text(settings.hotkeys.dictate_en)
        self._cancel_edit.set_config_text(settings.hotkeys.cancel)
        self._quick_lookup_edit.set_config_text(settings.hotkeys.quick_lookup)
        self._selection_translate_edit.set_config_text(settings.hotkeys.selection_translate)
        # ASR
        self._max_record_spin.setValue(int(settings.asr.max_record_seconds))
        self._instant_recording_check.setChecked(settings.asr.instant_recording)
        self._select_mic(settings.asr.mic_device_index)
        self.refresh_model_status()
        # 翻译
        self._profiles = list(settings.translation.profiles)
        self._rebuild_profile_combo(settings.translation.active_profile)
        self._new_profile_draft = False
        # 个性化
        self._suffix_check.setChecked(settings.personalize.suffix_enabled)
        self._suffix_edit.setText(settings.personalize.suffix_text)
        self._suffix_edit.setEnabled(settings.personalize.suffix_enabled)
        self._set_rewrite_style(settings.personalize.rewrite_style)
        # 划词翻译
        self._selection_enabled_check.setChecked(settings.selection_translate.enabled)
        self._selection_auto_check.setChecked(settings.selection_translate.auto_trigger)
        self._selection_auto_check.setEnabled(settings.selection_translate.enabled)
        self._selection_timeout_spin.setValue(
            int(settings.selection_translate.popup_timeout_s)
        )

    def apply_to_settings(self, settings: AppSettings) -> AppSettings:
        """把控件当前值写回 settings 并返回（在副本上修改）。"""
        import dataclasses

        settings = dataclasses.replace(settings)
        settings.enabled = self._enable_check.isChecked()
        autostart_checked = self._autostart_check.isChecked()
        try:
            set_launch_at_login(autostart_checked)
        except OSError:
            QMessageBox.warning(
                self,
                "开机启动",
                "无法修改开机启动设置（注册表写入失败），请检查权限或手动配置。",
            )
            try:
                self._autostart_check.setChecked(is_launch_at_login_enabled())
            except Exception:  # noqa: BLE001
                self._autostart_check.setChecked(False)
        ui = dataclasses.replace(
            settings.ui,
            launch_at_login=autostart_checked,
            result_ui=self._current_result_ui(),
        )
        settings.ui = ui
        hk = dataclasses.replace(
            settings.hotkeys,
            toggle_enabled=self._toggle_hotkey.config_text() or settings.hotkeys.toggle_enabled,
            dictate_zh=self._dictate_zh_edit.config_text() or settings.hotkeys.dictate_zh,
            dictate_en=self._dictate_en_edit.config_text() or settings.hotkeys.dictate_en,
            cancel=self._cancel_edit.config_text() or settings.hotkeys.cancel,
            quick_lookup=(
                self._quick_lookup_edit.config_text()
                or settings.hotkeys.quick_lookup
            ),
            selection_translate=(
                self._selection_translate_edit.config_text()
                or settings.hotkeys.selection_translate
            ),
        )
        settings.hotkeys = hk
        asr = dataclasses.replace(
            settings.asr,
            max_record_seconds=int(self._max_record_spin.value()),
            instant_recording=self._instant_recording_check.isChecked(),
            mic_device_index=self._current_mic_index(),
        )
        settings.asr = asr
        self._flush_edit_to_active_profile()
        active = self._profile_combo.currentText()
        if not active:
            active = ""
        tr = dataclasses.replace(
            settings.translation,
            profiles=list(self._profiles),
            active_profile=active,
        )
        settings.translation = tr
        personalize = dataclasses.replace(
            settings.personalize,
            suffix_enabled=self._suffix_check.isChecked(),
            suffix_text=self._suffix_edit.text(),
            rewrite_style=self._current_rewrite_style(),
        )
        settings.personalize = personalize
        selection_translate = dataclasses.replace(
            settings.selection_translate,
            enabled=self._selection_enabled_check.isChecked(),
            auto_trigger=self._selection_auto_check.isChecked(),
            popup_timeout_s=int(self._selection_timeout_spin.value()),
        )
        settings.selection_translate = selection_translate
        return settings

    # ==================== 一键重置 ====================

    def reset_to_defaults(self) -> None:
        """把非翻译页控件重置为 dataclass 默认值（仅改 UI，不持久化）。"""
        default_ui = UiSettings()
        default_hk = HotkeySettings()
        default_asr = AsrSettings()
        default_personalize = PersonalizeSettings()
        default_selection = SelectionTranslateSettings()
        # 常规
        self._enable_check.setChecked(True)
        self._autostart_check.setChecked(default_ui.launch_at_login)
        self._set_result_ui(default_ui.result_ui)
        self._toggle_hotkey.set_config_text(default_hk.toggle_enabled)
        # 快捷键
        self._dictate_zh_edit.set_config_text(default_hk.dictate_zh)
        self._dictate_en_edit.set_config_text(default_hk.dictate_en)
        self._cancel_edit.set_config_text(default_hk.cancel)
        self._quick_lookup_edit.set_config_text(default_hk.quick_lookup)
        self._selection_translate_edit.set_config_text(default_hk.selection_translate)
        # ASR
        self._max_record_spin.setValue(int(default_asr.max_record_seconds))
        self._instant_recording_check.setChecked(default_asr.instant_recording)
        self._select_mic(default_asr.mic_device_index)
        # 个性化
        self._suffix_check.setChecked(default_personalize.suffix_enabled)
        self._suffix_edit.setText(default_personalize.suffix_text)
        self._suffix_edit.setEnabled(default_personalize.suffix_enabled)
        self._set_rewrite_style(default_personalize.rewrite_style)
        # 划词翻译
        self._selection_enabled_check.setChecked(default_selection.enabled)
        self._selection_auto_check.setChecked(default_selection.auto_trigger)
        self._selection_auto_check.setEnabled(default_selection.enabled)
        self._selection_timeout_spin.setValue(int(default_selection.popup_timeout_s))
        # 状态提示
        self._reset_status_label.setText("已恢复默认值，点击应用生效")

    def _on_reset_clicked(self) -> None:
        btn = QMessageBox.question(
            self,
            "恢复默认设置",
            "将恢复常规、快捷键、语音识别、界面、个性化、划词翻译设置为默认值，翻译配置不受影响。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if btn == QMessageBox.StandardButton.Yes:
            self.reset_to_defaults()

    def _on_clear_data_clicked(self) -> None:
        """点击「清除全部数据并退出…」：弹确认框，确认才 emit clear_data_requested。"""
        btn = QMessageBox.warning(
            self,
            "清除全部数据并退出",
            "此操作将删除全部设置（包含翻译 API Key）、日志与已下载的模型，"
            "且应用将立即退出（之后需重新下载模型）。\n\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if btn == QMessageBox.StandardButton.Yes:
            self.clear_data_requested.emit()

    # ==================== ASR 实际设备显示 ====================

    def set_actual_device(self, device: AsrDevice | None, providers: list[str]) -> None:
        """由 application 层在 ASR 初始化完成后调用（queued 主线程）。"""
        if device is None:
            self._actual_device_label.setText("（未初始化）")
            return
        providers_str = ", ".join(str(p) for p in providers) if providers else str(device)
        self._actual_device_label.setText(f"{device.value} · {providers_str}")

    def set_recheck_loading(self, loading: bool) -> None:
        """切换「重新检测硬件」按钮加载态。"""
        self._recheck_btn.setEnabled(not loading)
        self._recheck_btn.setText("加载中…" if loading else "重新检测硬件")

    # ==================== 翻译测试结果 / 可用模型拉取 ====================

    def set_test_connection_result(self, ok: bool, message: str) -> None:
        """由 application 层在后台测试完成后调用（queued 主线程）。"""
        prefix = "成功：" if ok else "失败："
        self._test_result_label.setText(prefix + message)
        self._test_conn_btn.setEnabled(True)
        self._test_conn_btn.setText("测试连接")

    def set_fetched_models(
        self,
        success: bool,
        models: list[str],
        message: str,
        silent: bool = False,
        base_url: str | None = None,
    ) -> None:
        """由 application 层在后台拉取 /models 完成后调用（queued 主线程）。"""
        self._pending_fetch_key = None
        if self._fetching_models or not silent:
            self._set_fetch_models_loading(False)

        # 若用户已切回内置免费引擎，忽略迟到结果。
        if not self._base_url_edit.isEnabled():
            return

        # 若用户在拉取期间已切换自定义档案（Base URL 变化），忽略旧档案迟到结果。
        current_base_url = self._base_url_edit.text().strip()
        if base_url is not None and base_url.strip() != current_base_url:
            return

        if not success:
            if not silent:
                detail = message.strip() if message else "请手动输入模型 ID"
                self._test_result_label.setText(f"获取模型失败：{detail}")
            return

        self._last_fetched_models_key = (
            self._base_url_edit.text().strip(),
            self._api_key_edit.text().strip(),
        )
        cleaned = [m.strip() for m in models if isinstance(m, str) and m.strip()]
        current_text = self._model_edit.text().strip()

        was = self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for m in cleaned:
            self._model_combo.addItem(m)
        if current_text:
            idx = self._model_combo.findText(current_text)
            if idx >= 0:
                self._model_combo.setCurrentIndex(idx)
            else:
                self._model_combo.setEditText(current_text)
        elif cleaned:
            self._model_combo.setCurrentIndex(0)
        self._model_combo.blockSignals(was)

        if cleaned:
            if not silent or not self._test_result_label.text():
                self._test_result_label.setText(f"已获取 {len(cleaned)} 个模型")
        elif not silent:
            self._test_result_label.setText("未获取到可用模型，请手动输入模型 ID")

    def _set_fetch_models_loading(self, loading: bool) -> None:
        self._fetching_models = loading
        can_enable = (not loading) and self._base_url_edit.isEnabled()
        self._fetch_models_btn.setEnabled(can_enable)
        self._fetch_models_btn.setText("获取中…" if loading else "获取模型")

    @staticmethod
    def _is_valid_http_url(url: str) -> bool:
        raw = (url or "").strip()
        if not raw:
            return False
        parsed = urlparse(raw)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    def _on_fetch_models_clicked(self) -> None:
        if not self._base_url_edit.isEnabled():
            return
        base_url = self._base_url_edit.text().strip()
        if not base_url:
            self._test_result_label.setText("请先填写 Base URL")
            return
        api_key = self._api_key_edit.text().strip()
        self._pending_fetch_key = (base_url, api_key)
        self._set_fetch_models_loading(True)
        self._test_result_label.setText("正在获取模型列表…")
        self.fetch_models_requested.emit(base_url, api_key, False)

    def _on_endpoint_editing_finished(self) -> None:
        """Base URL 或 API Key 编辑完成时，满足条件则自动静默拉取可用模型。"""
        if not self._base_url_edit.isEnabled() or self._custom_form_container.isHidden():
            return
        base_url = self._base_url_edit.text().strip()
        if not self._is_valid_http_url(base_url):
            return
        api_key = self._api_key_edit.text().strip()
        key = (base_url, api_key)
        if key == self._last_fetched_models_key or key == self._pending_fetch_key:
            return
        self._pending_fetch_key = key
        self.fetch_models_requested.emit(base_url, api_key, True)

    # ==================== 模型状态 / 下载 ====================

    def _set_model_badge_tone(self, tone: str) -> None:
        self._model_status_label.setProperty("tone", tone)
        self._model_status_label.style().unpolish(self._model_status_label)
        self._model_status_label.style().polish(self._model_status_label)

    def _sync_download_btn_for_ready(self, ready: bool) -> None:
        self._download_model_btn.setText("重新下载模型" if ready else "下载模型")
        self._download_model_btn.show()

    def refresh_model_status(self) -> None:
        """刷新模型状态标签与下载按钮。"""
        from ..asr.providers import default_model_dir, models_ready

        if self._model_progress.isVisible():
            return
        model_dir = default_model_dir()
        ready = models_ready(model_dir)
        if ready:
            self._model_status_label.setText("已安装")
            self._model_status_label.setToolTip(str(model_dir))
            self._set_model_badge_tone("success")
        else:
            self._model_status_label.setText("未安装")
            self._model_status_label.setToolTip("")
            self._set_model_badge_tone("warning")
        self._sync_download_btn_for_ready(ready)

    def set_model_download_progress(self, percent: int) -> None:
        if percent < 0:
            self._model_progress.setRange(0, 0)
        else:
            self._model_progress.setRange(0, 100)
            self._model_progress.setValue(max(0, min(100, percent)))
        self._model_progress.show()
        self._download_model_btn.setEnabled(False)
        self._download_model_btn.setText("下载中…")

    def on_model_download_complete(self) -> None:
        self._model_progress.hide()
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(0)
        self._download_model_btn.setEnabled(True)
        self.refresh_model_status()

    def on_model_download_failed(self, message: str) -> None:
        from ..asr.providers import default_model_dir, models_ready

        self._model_progress.hide()
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(0)
        self._download_model_btn.setEnabled(True)
        self._model_status_label.setText("下载失败：" + message)
        self._set_model_badge_tone("danger")
        self._sync_download_btn_for_ready(models_ready(default_model_dir()))

    def mark_model_load_failed(self, message: str) -> None:
        self._model_status_label.setText(
            f"模型加载失败：{message}，可点击「重新下载模型」修复"
        )
        self._set_model_badge_tone("danger")
        self._download_model_btn.setEnabled(True)
        self._sync_download_btn_for_ready(True)

    # ==================== 按钮事件 ====================

    def _on_ok(self) -> None:
        new_settings = self.apply_to_settings(self._last_loaded)
        self.applied.emit(new_settings)
        self.hide()

    def _on_apply(self) -> None:
        new_settings = self.apply_to_settings(self._last_loaded)
        self.applied.emit(new_settings)

    def _on_recheck_clicked(self) -> None:
        self._recheck_btn.setEnabled(False)
        self._recheck_btn.setText("加载中…")
        self.recheck_hardware_requested.emit()

    def _on_test_popup_clicked(self) -> None:
        self.test_popup_requested.emit()

    def _on_download_model_clicked(self) -> None:
        self._download_model_btn.setEnabled(False)
        self._download_model_btn.setText("下载中…")
        self._model_progress.setRange(0, 0)
        self._model_progress.setValue(0)
        self._model_progress.show()
        self.model_download_requested.emit()

    def _on_test_conn_clicked(self) -> None:
        profile = self._current_test_profile()
        if profile is None:
            return
        self._test_conn_btn.setEnabled(False)
        self._test_conn_btn.setText("测试中…")
        self._test_result_label.setText("测试中…")
        self.test_connection_requested.emit(profile)

    def _on_save_new_clicked(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._test_result_label.setText("请填写配置名称")
            return
        profile = self._profile_from_edit(name=name)
        if not profile.base_url or not profile.model:
            self._test_result_label.setText("请填写 Base URL 和模型")
            return
        replaced = False
        for i, p in enumerate(self._profiles):
            if p.name == profile.name:
                self._profiles[i] = profile
                replaced = True
                break
        if not replaced:
            self._profiles.append(profile)
        self._rebuild_profile_combo(profile.name)
        self._new_profile_draft = False

    def _on_new_profile_clicked(self) -> None:
        self._activate_user_edit_mode()
        self._clear_edit()
        self._name_edit.setFocus()
        self._new_profile_draft = True
        self._test_result_label.setText("正在新建配置：填写后点击『保存为新配置』")
        self._sync_delete_profile_btn()

    def _on_delete_profile_clicked(self) -> None:
        name = self._profile_combo.currentText()
        if name in _BUILTIN_NAMES or not any(p.name == name for p in self._profiles):
            return
        btn = QMessageBox.warning(
            self,
            "删除翻译配置",
            f"将删除配置「{name}」，删除结果在点击「应用/确定」后生效。\n\n确定删除？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return
        self._profiles = [p for p in self._profiles if p.name != name]
        fallback = self._profiles[0].name if self._profiles else ""
        self._rebuild_profile_combo(fallback)

    # ==================== 翻译 profiles 编辑辅助 ====================

    def _on_profile_selected(self, _index: int) -> None:
        name = self._profile_combo.currentText()
        self._sync_delete_profile_btn()
        self._new_profile_draft = False
        self._test_result_label.setText("")
        if name in _BUILTIN_NAMES:
            self._builtin_hint_label.show()
            self._custom_form_container.hide()
            self._save_new_btn.hide()
            self._set_edit_enabled(False)
            self._save_new_btn.setEnabled(False)
            self._set_fetch_models_loading(False)
            self._load_profile_to_edit(self._builtin_profile(name))
            return
        self._activate_user_edit_mode()
        for p in self._profiles:
            if p.name == name:
                self._load_profile_to_edit(p)
                return
        self._clear_edit()

    def _rebuild_profile_combo(self, active_name: str) -> None:
        was = self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for p in _BUILTIN_PROFILES:
            self._profile_combo.addItem(p.name)
        for p in self._profiles:
            self._profile_combo.addItem(p.name)
        idx = self._profile_combo.findText(active_name) if active_name else -1
        if active_name and idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        elif self._profile_combo.count() > 0:
            self._profile_combo.setCurrentIndex(0)
        self._profile_combo.blockSignals(was)
        self._on_profile_selected(self._profile_combo.currentIndex())

    def _flush_edit_to_active_profile(self) -> None:
        if self._new_profile_draft:
            return
        name = self._profile_combo.currentText()
        if not name or name in _BUILTIN_NAMES:
            return
        profile = self._profile_from_edit(name=name)
        for i, p in enumerate(self._profiles):
            if p.name == name:
                self._profiles[i] = profile
                return
        self._profiles.append(profile)

    def _load_profile_to_edit(self, profile: TranslationProfile) -> None:
        self._last_fetched_models_key = None
        self._pending_fetch_key = None
        self._name_edit.setText(profile.name)
        self._base_url_edit.setText(profile.base_url)
        was = self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.setEditText(profile.model)
        self._model_combo.blockSignals(was)
        self._api_key_edit.setText(profile.api_key or "")

    def _clear_edit(self) -> None:
        self._last_fetched_models_key = None
        self._pending_fetch_key = None
        self._name_edit.clear()
        self._base_url_edit.clear()
        was = self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_edit.clear()
        self._model_combo.blockSignals(was)
        self._api_key_edit.clear()

    def _set_edit_enabled(self, enabled: bool) -> None:
        for w in (
            self._name_edit,
            self._base_url_edit,
            self._model_combo,
            self._fetch_models_btn,
            self._api_key_edit,
        ):
            w.setEnabled(enabled)

    def _activate_user_edit_mode(self) -> None:
        self._builtin_hint_label.hide()
        self._custom_form_container.show()
        self._save_new_btn.show()
        self._set_edit_enabled(True)
        self._set_fetch_models_loading(False)
        self._save_new_btn.setEnabled(True)

    def _sync_delete_profile_btn(self) -> None:
        name = self._profile_combo.currentText()
        self._delete_profile_btn.setEnabled(
            name not in _BUILTIN_NAMES and any(p.name == name for p in self._profiles)
        )

    def _profile_from_edit(self, name: str | None = None) -> TranslationProfile:
        return TranslationProfile(
            name=(name if name is not None else self._name_edit.text().strip()),
            base_url=self._base_url_edit.text().strip(),
            model=self._model_edit.text().strip(),
            api_key=self._api_key_edit.text() or None,
            timeout=60.0,
            backend_type="openai_compatible",
        )

    @staticmethod
    def _builtin_profile(name: str) -> TranslationProfile:
        for p in _BUILTIN_PROFILES:
            if p.name == name:
                return p
        return _BUILTIN_PROFILES[0]

    def _current_test_profile(self) -> TranslationProfile | None:
        name = self._profile_combo.currentText()
        if name in _BUILTIN_NAMES:
            return self._builtin_profile(name)
        profile = self._profile_from_edit()
        if not profile.base_url or not profile.model:
            self._test_result_label.setText("请填写 Base URL 和模型")
            return None
        return profile

    # ==================== 结果展示方式辅助 ====================

    def _set_result_ui(self, value: str) -> None:
        idx = self._result_ui_combo.findData(value)
        if idx < 0:
            idx = self._result_ui_combo.findData("popup")
        if idx >= 0:
            self._result_ui_combo.setCurrentIndex(idx)

    def _current_result_ui(self) -> str:
        data = self._result_ui_combo.currentData()
        return data if data in ("popup", "overlay") else "popup"

    # ==================== ASR 控件辅助 ====================

    def _current_mic_index(self) -> int | None:
        data = self._mic_combo.currentData()
        return data if isinstance(data, int) else None

    def _select_mic(self, device_index: int | None) -> None:
        if device_index is None:
            self._mic_combo.setCurrentIndex(0)
            return
        idx = self._mic_combo.findData(device_index)
        if idx >= 0:
            self._mic_combo.setCurrentIndex(idx)
        else:
            self._mic_combo.setCurrentIndex(0)
