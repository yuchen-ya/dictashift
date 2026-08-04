"""设置窗口（五页 QTabWidget）。

页面结构：
1. 常规：启用语音输入、开机启动(仅保存设置)、结果展示方式(右下角弹窗/常驻悬浮窗)、
   全局禁用快捷键、恢复默认设置按钮（重置常规/快捷键/语音识别/界面/个性化/划词翻译，
   不影响翻译配置）。
2. 快捷键：中文输入(f8)、中文→英文(f9)、取消当前任务(esc)、划词翻译开关(f7)。
3. 语音识别：模型(已安装/未安装 状态 + 下载按钮 + 进度条)、实际运行设备(只读)、
   麦克风、最长录音秒数、重新检测硬件、测试识别(禁用占位)。
   注：推理设备/GPU 设备控件已随 GPU 路线移除（设备固定为 AUTO/CPU，等价）。
4. 翻译：当前配置 ComboBox（内置免登录 Google/Bing + 用户 LLM profiles）、
   编辑区(base_url/model/api_key/temperature，仅作用于用户 LLM 配置)、
   保存为新配置、测试连接(后台)；划词翻译分组（总开关/自动弹出/弹窗显示时长）。
5. 个性化：句尾后缀（开关 + 文本，纯字符串拼接）、语气改写（关闭/毒舌/傲娇
   互斥单选，复用翻译页活动档案的 LLM 配置）。

线程模型：
- 控件值 <-> AppSettings 双向绑定（load_from_settings / apply_to_settings）。
- 重后台工作（ASR initialize、翻译测试连接、模型下载）通过 Qt signal 交给
  application 层在后台线程执行，结果再 queued 回 UI；UI 线程不做网络/模型工作。
- 「开机启动」勾选状态直接读写注册表（is_launch_at_login_enabled），与
  settings.ui.launch_at_login 解耦（后者仅作记录）；apply 时调用
  set_launch_at_login 真正写注册表，失败弹 QMessageBox 警告。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..asr.providers import AsrDevice
from ..audio.devices import list_input_devices
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
from ..resources import app_icon_png_path
from ..translation import TranslationProfile, builtin_free_profiles, builtin_profile_names
from .hotkey_editor import HotkeyEdit
from .theme import app_stylesheet


# 内置免登录翻译项顺序（与「当前翻译配置」ComboBox 内置段顺序一致）。
_BUILTIN_PROFILES = list(builtin_free_profiles())
_BUILTIN_NAMES = builtin_profile_names()


class SettingsWindow(QWidget):
    """设置窗口。

    信号（application 层连接）：
    - applied(settings)：用户点击「应用」或「确定」时发出，携带最新设置副本。
    - recheck_hardware_requested()：用户点击「重新检测硬件」（无参，固定重新初始化）。
    - test_connection_requested(profile)：用户点击「测试连接」（后台执行）。
    - model_download_requested()：用户点击「下载模型」（后台执行，无参）。
    - test_popup_requested()：用户点击「测试右下角弹窗」（让 application 层用样例文本
      调 ResultPopup.show_result，验证弹窗可见性与复制功能）。
    - clear_data_requested()：用户点击「清除全部数据并退出…」并确认二次弹窗后发出
      （application 层停止服务、删除应用数据目录、退出）。
    """

    applied = Signal(AppSettings)
    recheck_hardware_requested = Signal()
    test_connection_requested = Signal(object)  # TranslationProfile
    model_download_requested = Signal()
    test_popup_requested = Signal()
    clear_data_requested = Signal()  # 用户确认「清除全部数据并退出」

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DictaShift 设置")
        self.resize(560, 520)

        # 最近一次 load_from_settings 缓存的 settings 副本，供 ok/apply 作为基底。
        self._last_loaded: AppSettings = AppSettings()

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_general_tab(), "常规")
        self._tabs.addTab(self._build_hotkeys_tab(), "快捷键")
        self._tabs.addTab(self._build_asr_tab(), "语音识别")
        self._tabs.addTab(self._build_translation_tab(), "翻译")
        self._tabs.addTab(self._build_personalize_tab(), "个性化")

        # 底部按钮
        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("确定")
        self._apply_btn = QPushButton("应用")
        self._cancel_btn = QPushButton("取消")
        btn_row.addStretch(1)
        btn_row.addWidget(self._ok_btn)
        btn_row.addWidget(self._apply_btn)
        btn_row.addWidget(self._cancel_btn)

        root = QVBoxLayout(self)
        root.addLayout(self._build_header())
        # header 与 tab 之间的细分隔线（样式见 theme.py 的 QFrame#headerDivider）。
        divider = QFrame(self)
        divider.setObjectName("headerDivider")
        divider.setFrameShape(QFrame.HLine)
        root.addWidget(divider)
        root.addWidget(self._tabs, stretch=1)
        root.addLayout(btn_row)

        # 整体深色主题（见 ui/theme.py）；危险按钮等个别样式仍为局部内联。
        self.setStyleSheet(app_stylesheet())

        self._ok_btn.clicked.connect(self._on_ok)
        self._apply_btn.clicked.connect(self._on_apply)
        self._cancel_btn.clicked.connect(self.hide)

    def _build_header(self) -> QHBoxLayout:
        """顶部 header：左侧窗口标题文字「DictaShift」，右上角应用 logo。

        logo 资源缺失（如开发期未运行 scripts/gen_icon.py）时隐藏 logo 标签，
        不影响其余界面。
        """
        header = QHBoxLayout()
        title_label = QLabel("DictaShift", self)
        title_label.setObjectName("appTitle")
        header.addWidget(title_label)
        header.addStretch(1)

        self._logo_label = QLabel(self)
        self._logo_label.setObjectName("appLogo")
        logo_pm = QPixmap(str(app_icon_png_path()))
        if logo_pm.isNull():
            self._logo_label.hide()
        else:
            self._logo_label.setPixmap(
                logo_pm.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        header.addWidget(self._logo_label)
        return header

    def showEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """窗口显示时刷新一次模型状态（用户可能刚下载完模型）。"""
        super().showEvent(event)
        try:
            self.refresh_model_status()
        except Exception:  # noqa: BLE001
            pass

    # ==================== 常规页 ====================

    def _build_general_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self._enable_check = QCheckBox("启用语音输入", page)
        form.addRow(self._enable_check)

        self._autostart_check = QCheckBox("开机启动", page)
        form.addRow(self._autostart_check)

        self._result_ui_combo = QComboBox(page)
        self._result_ui_combo.addItem(
            "右下角弹窗（失败时弹出，平时无悬浮窗）", "popup"
        )
        self._result_ui_combo.addItem(
            "常驻悬浮窗（置顶显示状态与结果）", "overlay"
        )
        # 紧邻结果展示方式：让用户手动验证右下角弹窗可见性与复制功能
        # （诊断「没有光标时弹窗也不弹、文本无处可复制」）。
        self._test_popup_btn = QPushButton("测试右下角弹窗", page)
        self._test_popup_btn.clicked.connect(self._on_test_popup_clicked)
        result_ui_row = QHBoxLayout()
        result_ui_row.addWidget(self._result_ui_combo, stretch=1)
        result_ui_row.addWidget(self._test_popup_btn)
        form.addRow("结果展示方式：", result_ui_row)

        form.addRow(QLabel("全局禁用快捷键：", page))
        self._toggle_hotkey = HotkeyEdit(page)
        form.addRow(self._toggle_hotkey)

        # 恢复默认设置：重置常规/快捷键/语音识别/界面为默认值（不影响翻译配置）。
        self._reset_btn = QPushButton("恢复默认设置", page)
        self._reset_btn.clicked.connect(self._on_reset_clicked)
        self._reset_status_label = QLabel("", page)
        reset_row = QHBoxLayout()
        reset_row.addWidget(self._reset_btn)
        reset_row.addWidget(self._reset_status_label)
        reset_row.addStretch(1)
        form.addRow(reset_row)

        # 危险操作：清除全部数据并退出（删除 %APPDATA%\\DictaShift 全部内容后退出）。
        self._clear_data_btn = QPushButton("清除全部数据并退出…", page)
        self._clear_data_btn.setObjectName("clear_data_btn")
        self._clear_data_btn.setStyleSheet(
            "QPushButton { color: #ff6b6b; border: 1px solid #c0392b; border-radius: 4px; padding: 4px 10px; }"
            "QPushButton:hover { background-color: rgba(192, 57, 43, 40); }"
        )
        self._clear_data_btn.clicked.connect(self._on_clear_data_clicked)
        clear_row = QHBoxLayout()
        clear_row.addWidget(self._clear_data_btn)
        clear_row.addStretch(1)
        form.addRow(clear_row)

        return page

    # ==================== 快捷键页 ====================

    def _build_hotkeys_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self._dictate_zh_edit = HotkeyEdit(page)
        form.addRow("中文输入：", self._dictate_zh_edit)

        self._dictate_en_edit = HotkeyEdit(page)
        form.addRow("中文 → 英文：", self._dictate_en_edit)

        self._cancel_edit = HotkeyEdit(page)
        form.addRow("取消当前任务：", self._cancel_edit)

        self._selection_translate_edit = HotkeyEdit(page)
        form.addRow("划词翻译开关：", self._selection_translate_edit)

        return page

    # ==================== 语音识别页 ====================

    def _build_asr_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        # 模型区：状态标签 + 下载按钮 + 进度条（默认隐藏）。
        self._model_status_label = QLabel("（检测中…）", page)
        self._model_status_label.setWordWrap(True)
        self._download_model_btn = QPushButton("下载模型", page)
        self._download_model_btn.clicked.connect(self._on_download_model_clicked)
        self._model_progress = QProgressBar(page)
        self._model_progress.setRange(0, 100)
        self._model_progress.hide()
        model_row = QHBoxLayout()
        model_row.addWidget(self._model_status_label, stretch=1)
        model_row.addWidget(self._download_model_btn)
        form.addRow("模型：", model_row)
        form.addRow("", self._model_progress)

        self._actual_device_label = QLabel("（未初始化）", page)
        form.addRow("实际运行设备：", self._actual_device_label)

        self._mic_combo = QComboBox(page)
        self._mic_combo.addItem("系统默认设备")
        for dev in list_input_devices():
            self._mic_combo.addItem(
                f"{dev['name']} (#{dev['index']})", dev["index"]
            )
        form.addRow("麦克风：", self._mic_combo)

        self._max_record_spin = QSpinBox(page)
        self._max_record_spin.setRange(5, 120)
        self._max_record_spin.setSuffix(" 秒")
        form.addRow("最长录音秒数：", self._max_record_spin)

        btn_row = QHBoxLayout()
        self._recheck_btn = QPushButton("重新检测硬件", page)
        self._recheck_btn.clicked.connect(self._on_recheck_clicked)
        btn_row.addWidget(self._recheck_btn)

        self._test_asr_btn = QPushButton("测试识别", page)
        self._test_asr_btn.setEnabled(False)  # 第一版禁用态占位
        btn_row.addWidget(self._test_asr_btn)
        btn_row.addStretch(1)
        form.addRow(btn_row)

        return page

    # ==================== 翻译页 ====================

    def _build_translation_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)

        # 配置行：标签 + 下拉（拉伸占满）+「＋新建」+「删除」。
        self._profile_combo = QComboBox(page)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        self._new_profile_btn = QPushButton("＋新建", page)
        self._new_profile_btn.clicked.connect(self._on_new_profile_clicked)
        # 删除为危险操作，沿用常规页 clear_data_btn 的内联红样式。
        self._delete_profile_btn = QPushButton("删除", page)
        self._delete_profile_btn.setStyleSheet(
            "QPushButton { color: #ff6b6b; border: 1px solid #c0392b; border-radius: 4px; padding: 4px 10px; }"
            "QPushButton:hover { background-color: rgba(192, 57, 43, 40); }"
        )
        # 初始未加载任何配置，删除不可用（随后由 _on_profile_selected 同步）。
        self._delete_profile_btn.setEnabled(False)
        self._delete_profile_btn.clicked.connect(self._on_delete_profile_clicked)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("当前翻译配置：", page))
        profile_row.addWidget(self._profile_combo, stretch=1)
        profile_row.addWidget(self._new_profile_btn)
        profile_row.addWidget(self._delete_profile_btn)
        outer.addLayout(profile_row)

        # 内置项选中时显示的提示标签（免登录服务，无需配置）。
        self._builtin_hint_label = QLabel("免登录翻译服务，无需配置", page)
        outer.addWidget(self._builtin_hint_label)

        edit_group_label = QLabel("编辑当前配置：", page)
        outer.addWidget(edit_group_label)

        form = QFormLayout()
        self._name_edit = QLineEdit(page)
        form.addRow("配置名称：", self._name_edit)

        self._base_url_edit = QLineEdit(page)
        self._base_url_edit.setPlaceholderText("https://api.openai.com/v1 或 http://127.0.0.1:19120/v1")
        form.addRow("Base URL：", self._base_url_edit)

        self._model_edit = QLineEdit(page)
        form.addRow("模型：", self._model_edit)

        self._api_key_edit = QLineEdit(page)
        self._api_key_edit.setEchoMode(QLineEdit.Password)
        form.addRow("API Key：", self._api_key_edit)

        self._temperature_spin = QDoubleSpinBox(page)
        self._temperature_spin.setRange(0.0, 2.0)
        self._temperature_spin.setSingleStep(0.1)
        self._temperature_spin.setValue(0.3)
        form.addRow("Temperature：", self._temperature_spin)

        outer.addLayout(form)

        btn_row = QHBoxLayout()
        self._save_new_btn = QPushButton("保存为新配置", page)
        self._save_new_btn.clicked.connect(self._on_save_new_clicked)
        self._test_conn_btn = QPushButton("测试连接", page)
        self._test_conn_btn.clicked.connect(self._on_test_conn_clicked)
        btn_row.addWidget(self._save_new_btn)
        btn_row.addWidget(self._test_conn_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        self._test_result_label = QLabel("", page)
        outer.addWidget(self._test_result_label)

        # ---- 划词翻译组：总开关 + 自动弹出 + 弹窗显示时长 ----
        # 值绑定到 settings.selection_translate（随「应用/确定」生效）；
        # 开关快捷键在「快捷键」页录入（默认 F7，与总开关联动）。
        selection_divider = QFrame(page)
        selection_divider.setObjectName("headerDivider")
        selection_divider.setFrameShape(QFrame.HLine)
        outer.addWidget(selection_divider)
        outer.addWidget(QLabel("划词翻译：", page))
        selection_form = QFormLayout()
        self._selection_enabled_check = QCheckBox("启用划词翻译", page)
        selection_form.addRow(self._selection_enabled_check)
        self._selection_auto_check = QCheckBox("划选文字后自动弹出翻译", page)
        # 依赖总开关：未启用划词翻译时置灰。
        self._selection_enabled_check.toggled.connect(self._selection_auto_check.setEnabled)
        self._selection_auto_check.setEnabled(self._selection_enabled_check.isChecked())
        selection_form.addRow(self._selection_auto_check)
        self._selection_timeout_spin = QSpinBox(page)
        self._selection_timeout_spin.setRange(5, 60)
        self._selection_timeout_spin.setSuffix(" 秒")
        selection_form.addRow("弹窗显示时长：", self._selection_timeout_spin)
        outer.addLayout(selection_form)
        outer.addStretch(1)

        # 内存中维护的用户 LLM profiles 列表（编辑/保存时操作；apply 时同步到 settings）。
        # 内置免登录项不在此列表中，由 _rebuild_profile_combo 拼接到下拉顶部。
        self._profiles: list[TranslationProfile] = []

        # 是否处于「＋新建」草稿态：True 时编辑区内容尚未绑定到任何 profile，
        # apply/下拉切换时不应被 flush 回写为某已有配置（见 _flush_edit_to_active_profile）。
        self._new_profile_draft: bool = False

        return page

    # ==================== 个性化页 ====================

    def _build_personalize_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)

        # ---- 句尾后缀组：开关 + 文本（纯字符串拼接，不走大模型）----
        suffix_group_label = QLabel("句尾后缀：", page)
        outer.addWidget(suffix_group_label)
        suffix_form = QFormLayout()
        self._suffix_check = QCheckBox("启用句尾后缀", page)
        suffix_form.addRow(self._suffix_check)
        self._suffix_edit = QLineEdit(page)
        self._suffix_edit.setPlaceholderText("喵")
        # 输入框随开关 enable/disable（初始与开关默认未勾选一致）。
        self._suffix_check.toggled.connect(self._suffix_edit.setEnabled)
        self._suffix_edit.setEnabled(self._suffix_check.isChecked())
        suffix_form.addRow("后缀文本：", self._suffix_edit)
        outer.addLayout(suffix_form)

        # ---- 语气改写组：分段按钮组（关闭/毒舌/傲娇，互斥）----
        # 深色主题下 QRadioButton 选中态不明显，改用 checkable QPushButton + QButtonGroup，
        # 选中按钮由 theme.py 的 #segBtn[segmentBtn="selected"] 紫 accent 高亮显示。
        rewrite_group_label = QLabel("语气改写：", page)
        outer.addWidget(rewrite_group_label)
        seg_row = QHBoxLayout()
        seg_row.setSpacing(0)
        self._rewrite_off_btn = QPushButton("关闭", page)
        self._rewrite_off_btn.setObjectName("segmentBtn")
        self._rewrite_toxic_btn = QPushButton("毒舌", page)
        self._rewrite_toxic_btn.setObjectName("segmentBtn")
        self._rewrite_tsundere_btn = QPushButton("傲娇", page)
        self._rewrite_tsundere_btn.setObjectName("segmentBtn")
        for btn in (
            self._rewrite_off_btn,
            self._rewrite_toxic_btn,
            self._rewrite_tsundere_btn,
        ):
            btn.setCheckable(True)
            seg_row.addWidget(btn)
        seg_row.addStretch(1)
        outer.addLayout(seg_row)
        # 互斥：QButtonGroup(exclusive=True)。
        self._rewrite_group = QButtonGroup(page)
        self._rewrite_group.setExclusive(True)
        self._rewrite_group.addButton(self._rewrite_off_btn)
        self._rewrite_group.addButton(self._rewrite_toxic_btn)
        self._rewrite_group.addButton(self._rewrite_tsundere_btn)
        # 按钮被点击切换 checked 时，刷新 objectName 触发定向样式更新。
        self._rewrite_group.buttonClicked.connect(self._refresh_rewrite_segment_style)
        self._rewrite_off_btn.setChecked(True)
        self._refresh_rewrite_segment_style()

        # 说明：改写复用翻译页活动档案的 LLM 配置；非 OpenAI 兼容型自动跳过。
        hint_label = QLabel(
            "改写使用「翻译」页当前活动档案的大模型配置；"
            "活动档案需为 OpenAI 兼容型，否则改写自动跳过。",
            page,
        )
        hint_label.setWordWrap(True)
        outer.addWidget(hint_label)
        outer.addStretch(1)

        return page

    def _refresh_rewrite_segment_style(self, *_args) -> None:
        """按当前选中态刷新分段按钮的 segmentBtn 属性（驱动 theme.py 定向样式）。"""
        for btn, style in (
            (self._rewrite_off_btn, "selected" if self._rewrite_off_btn.isChecked() else ""),
            (self._rewrite_toxic_btn, "selected" if self._rewrite_toxic_btn.isChecked() else ""),
            (self._rewrite_tsundere_btn, "selected" if self._rewrite_tsundere_btn.isChecked() else ""),
        ):
            btn.setProperty("segmentBtn", style)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _set_rewrite_style(self, value: str) -> None:
        """把 rewrite_style 值同步到分段按钮；非法值回退到 off。"""
        if value == "toxic":
            self._rewrite_toxic_btn.setChecked(True)
        elif value == "tsundere":
            self._rewrite_tsundere_btn.setChecked(True)
        else:
            self._rewrite_off_btn.setChecked(True)
        self._refresh_rewrite_segment_style()

    def _current_rewrite_style(self) -> str:
        if self._rewrite_toxic_btn.isChecked():
            return "toxic"
        if self._rewrite_tsundere_btn.isChecked():
            return "tsundere"
        return "off"

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
        # 开机启动：以注册表实际状态为准。
        try:
            self._autostart_check.setChecked(is_launch_at_login_enabled())
        except Exception:  # noqa: BLE001
            # 注册表读取异常保守置为未勾选。
            self._autostart_check.setChecked(False)
        self._set_result_ui(settings.ui.result_ui)
        self._toggle_hotkey.set_config_text(settings.hotkeys.toggle_enabled)
        self._reset_status_label.setText("")
        # 快捷键
        self._dictate_zh_edit.set_config_text(settings.hotkeys.dictate_zh)
        self._dictate_en_edit.set_config_text(settings.hotkeys.dictate_en)
        self._cancel_edit.set_config_text(settings.hotkeys.cancel)
        self._selection_translate_edit.set_config_text(settings.hotkeys.selection_translate)
        # ASR
        self._max_record_spin.setValue(int(settings.asr.max_record_seconds))
        self._select_mic(settings.asr.mic_device_index)
        # 模型状态刷新一次。
        self.refresh_model_status()
        # 翻译
        self._profiles = list(settings.translation.profiles)
        self._rebuild_profile_combo(settings.translation.active_profile)
        # 载入新设置即丢弃任何残留草稿态。
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
        """把控件当前值写回 settings 并返回。

        在副本上修改，避免污染传入对象。

        「开机启动」勾选会真正写注册表（set_launch_at_login）；失败弹
        QMessageBox 警告，并把勾选状态回滚为注册表实际值。settings.ui.
        launch_at_login 仅作记录写入（无副作用）。
        """
        import dataclasses

        settings = dataclasses.replace(settings)
        settings.enabled = self._enable_check.isChecked()
        # 开机启动：真正写注册表。
        autostart_checked = self._autostart_check.isChecked()
        try:
            set_launch_at_login(autostart_checked)
        except OSError:
            QMessageBox.warning(
                self,
                "开机启动",
                "无法修改开机启动设置（注册表写入失败），请检查权限或手动配置。",
            )
            # 回滚复选框为注册表实际值。
            try:
                self._autostart_check.setChecked(is_launch_at_login_enabled())
            except Exception:  # noqa: BLE001
                self._autostart_check.setChecked(False)
        # UI 子配置（launch_at_login 仅作记录）
        ui = dataclasses.replace(
            settings.ui,
            launch_at_login=autostart_checked,
            result_ui=self._current_result_ui(),
        )
        settings.ui = ui
        # 快捷键
        hk = dataclasses.replace(
            settings.hotkeys,
            toggle_enabled=self._toggle_hotkey.config_text() or settings.hotkeys.toggle_enabled,
            dictate_zh=self._dictate_zh_edit.config_text() or settings.hotkeys.dictate_zh,
            dictate_en=self._dictate_en_edit.config_text() or settings.hotkeys.dictate_en,
            cancel=self._cancel_edit.config_text() or settings.hotkeys.cancel,
            selection_translate=(
                self._selection_translate_edit.config_text()
                or settings.hotkeys.selection_translate
            ),
        )
        settings.hotkeys = hk
        # ASR
        asr = dataclasses.replace(
            settings.asr,
            max_record_seconds=int(self._max_record_spin.value()),
            mic_device_index=self._current_mic_index(),
        )
        settings.asr = asr
        # 翻译：把编辑区当前内容写回当前选中的用户 profile，再同步列表/active。
        self._flush_edit_to_active_profile()
        # active 取自 ComboBox 选中项的 name；内置项 name 也合法（由 application 层解析）。
        active = self._profile_combo.currentText()
        # 若 ComboBox 为空（极端：无内置无用户），active 置空。
        if not active:
            active = ""
        tr = dataclasses.replace(
            settings.translation,
            profiles=list(self._profiles),
            active_profile=active,
        )
        settings.translation = tr
        # 个性化
        personalize = dataclasses.replace(
            settings.personalize,
            suffix_enabled=self._suffix_check.isChecked(),
            suffix_text=self._suffix_edit.text(),
            rewrite_style=self._current_rewrite_style(),
        )
        settings.personalize = personalize
        # 划词翻译
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
        """把非翻译页控件重置为 dataclass 默认值（仅改 UI，不持久化）。

        重置范围：常规(enabled=True + UiSettings 默认)、快捷键(HotkeySettings 默认)、
        语音识别(AsrSettings 默认)、个性化(PersonalizeSettings 默认)、
        划词翻译(SelectionTranslateSettings 默认)。
        翻译页内容（profiles / active / 编辑区）完全不动。
        用户仍需点「应用/确定」才落盘，与现有编辑行为一致。
        """
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
        self._selection_translate_edit.set_config_text(default_hk.selection_translate)
        # ASR
        self._max_record_spin.setValue(int(default_asr.max_record_seconds))
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
        """点击「清除全部数据并退出…」：弹确认框，确认才 emit clear_data_requested。

        QMessageBox 直接走模块级 QMessageBox.warning（便于测试 patch
        dictashift.ui.settings_window.QMessageBox）。取消什么都不做。
        """
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

    # ==================== 翻译测试结果 ====================

    def set_test_connection_result(self, ok: bool, message: str) -> None:
        """由 application 层在后台测试完成后调用（queued 主线程）。"""
        prefix = "成功：" if ok else "失败："
        self._test_result_label.setText(prefix + message)
        self._test_conn_btn.setEnabled(True)
        self._test_conn_btn.setText("测试连接")

    # ==================== 模型状态 / 下载 ====================

    def refresh_model_status(self) -> None:
        """刷新模型状态标签与下载按钮可见性。

        已安装：显示「已安装（<路径>）」并隐藏下载按钮；
        未安装：显示「未安装」并显示下载按钮（除非下载进行中）。
        """
        from ..asr.providers import default_model_dir, models_ready

        # 下载进行中：不刷新（避免覆盖进度文案）。
        if self._model_progress.isVisible():
            return
        model_dir = default_model_dir()
        if models_ready(model_dir):
            self._model_status_label.setText(f"已安装（{model_dir}）")
            self._download_model_btn.hide()
        else:
            self._model_status_label.setText("未安装")
            self._download_model_btn.show()

    def set_model_download_progress(self, percent: int) -> None:
        """由 application 层在下载进度回调（queued 主线程）中调用。

        percent<0 表示总大小未知，进度条以忙碌态呈现。
        """
        if percent < 0:
            # 忙碌态：范围 0,0。
            self._model_progress.setRange(0, 0)
        else:
            self._model_progress.setRange(0, 100)
            self._model_progress.setValue(max(0, min(100, percent)))
        self._model_progress.show()
        self._download_model_btn.setEnabled(False)
        self._download_model_btn.setText("下载中…")

    def on_model_download_complete(self) -> None:
        """下载成功（queued 主线程）：刷新状态、复位按钮与进度条。"""
        self._model_progress.hide()
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(0)
        self._download_model_btn.setEnabled(True)
        self._download_model_btn.setText("下载模型")
        self.refresh_model_status()

    def on_model_download_failed(self, message: str) -> None:
        """下载失败（queued 主线程）：恢复按钮、隐藏进度条、显示错误文案。"""
        self._model_progress.hide()
        self._model_progress.setRange(0, 100)
        self._model_progress.setValue(0)
        self._download_model_btn.setEnabled(True)
        self._download_model_btn.setText("下载模型")
        self._model_status_label.setText("下载失败：" + message)
        # 失败后仍按实际就绪态决定按钮可见性。
        from ..asr.providers import default_model_dir, models_ready

        if models_ready(default_model_dir()):
            self._download_model_btn.hide()
        else:
            self._download_model_btn.show()

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
        # 重新检测 = 重新初始化（设备固定 AUTO/0）。
        self.recheck_hardware_requested.emit()

    def _on_test_popup_clicked(self) -> None:
        """点击「测试右下角弹窗」：仅发信号给 application 层弹 ResultPopup。

        UI 自身不直接调用 ResultPopup（由 application 持有）。
        """
        self.test_popup_requested.emit()

    def _on_download_model_clicked(self) -> None:
        """点击「下载模型」：禁用按钮、显示进度条、发信号给 application 后台下载。

        UI 自身不发网络请求（AGENTS.md 11）。
        """
        self._download_model_btn.setEnabled(False)
        self._download_model_btn.setText("下载中…")
        # 忙碌态（总大小未知时保持），application 收到 Content-Length 后会切到百分比。
        self._model_progress.setRange(0, 0)
        self._model_progress.setValue(0)
        self._model_progress.show()
        self.model_download_requested.emit()

    def _on_test_conn_clicked(self) -> None:
        profile = self._current_test_profile()
        if profile is None:
            # 用户配置且字段缺失：_current_test_profile 已写错误提示
            return
        self._test_conn_btn.setEnabled(False)
        self._test_conn_btn.setText("测试中…")
        self._test_result_label.setText("测试中…")
        self.test_connection_requested.emit(profile)

    def _on_save_new_clicked(self) -> None:
        """把当前编辑区内容保存为一个新用户 LLM 配置（追加到 profiles）。

        仅在用户配置选中时可用（内置项选中时此按钮被禁用）。保存的 profile 固定
        backend_type="openai_compatible"。
        """
        name = self._name_edit.text().strip()
        if not name:
            self._test_result_label.setText("请填写配置名称")
            return
        profile = self._profile_from_edit(name=name)
        if not profile.base_url or not profile.model:
            self._test_result_label.setText("请填写 Base URL 和模型")
            return
        # 若同名已存在则替换，否则追加。
        replaced = False
        for i, p in enumerate(self._profiles):
            if p.name == profile.name:
                self._profiles[i] = profile
                replaced = True
                break
        if not replaced:
            self._profiles.append(profile)
        self._rebuild_profile_combo(profile.name)
        # 草稿已落定为真实 profile，退出草稿态。
        self._new_profile_draft = False

    def _on_new_profile_clicked(self) -> None:
        """点击「＋新建」：进入新建态，清空并启用编辑区，等待「保存为新配置」。

        不改动下拉框选中项、不插入临时项；填写后点「保存为新配置」才真正
        追加到 profiles（落盘仍走应用/确定时的现有路径）。
        """
        self._activate_user_edit_mode()
        self._clear_edit()  # 含 Temperature 复位默认值 0.30
        self._name_edit.setFocus()
        # 进入草稿态：未点「保存为新配置」前，编辑区内容不归属任何 profile，
        # 切换下拉/apply 时都不应被 flush 写回（见 _flush_edit_to_active_profile）。
        self._new_profile_draft = True
        self._test_result_label.setText("正在新建配置：填写后点击『保存为新配置』")
        # 下拉选中项未变，删除可用态按其重新同步一次。
        self._sync_delete_profile_btn()

    def _on_delete_profile_clicked(self) -> None:
        """点击「删除」：二次确认后从内存 profiles 移除当前选中的用户配置。

        仅用户自建配置可删（内置项/未匹配时按钮已禁用，此处防御性兜底）。
        不写盘，沿用「应用/确定」统一落盘的现有路径；取消不做任何改动。
        """
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
        # 回退选中：优先剩余的第一个用户配置，否则第一个内置项（active_name 为空时
        # _rebuild_profile_combo 自动选中 index 0）。
        fallback = self._profiles[0].name if self._profiles else ""
        self._rebuild_profile_combo(fallback)

    # ==================== 翻译 profiles 编辑辅助 ====================

    def _on_profile_selected(self, _index: int) -> None:
        """ComboBox 选择变化时载入对应 profile 到编辑区，并切换编辑区启用态。

        - 内置项：编辑区整体禁用、显示提示标签、「保存为新配置」禁用。
        - 用户项：编辑区启用、隐藏提示标签、「保存为新配置」启用。
        - 两个分支都会同步「删除」按钮可用态（仅用户自建配置可删）。
        """
        name = self._profile_combo.currentText()
        self._sync_delete_profile_btn()
        # 下拉切换即离开草稿态（草稿内容视为放弃，载入新选中项覆盖编辑区）。
        self._new_profile_draft = False
        if name in _BUILTIN_NAMES:
            self._builtin_hint_label.show()
            self._set_edit_enabled(False)
            self._save_new_btn.setEnabled(False)
            self._load_profile_to_edit(self._builtin_profile(name))
            return
        self._activate_user_edit_mode()
        for p in self._profiles:
            if p.name == name:
                self._load_profile_to_edit(p)
                return
        # 空选或未匹配：清空编辑区
        self._clear_edit()

    def _rebuild_profile_combo(self, active_name: str) -> None:
        """重建 ComboBox：内置项在前，用户 profiles 在后；选中 active_name。"""
        was = self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for p in _BUILTIN_PROFILES:
            self._profile_combo.addItem(p.name)
        for p in self._profiles:
            self._profile_combo.addItem(p.name)
        # 选中 active_name（内置项或用户项均可）；否则空选（index 0 即首个内置项）。
        idx = self._profile_combo.findText(active_name) if active_name else -1
        if active_name and idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        elif self._profile_combo.count() > 0:
            self._profile_combo.setCurrentIndex(0)
        self._profile_combo.blockSignals(was)
        # 触发一次编辑区载入/启用态切换
        self._on_profile_selected(self._profile_combo.currentIndex())

    def _flush_edit_to_active_profile(self) -> None:
        """把当前编辑区内容写回当前选中的用户 profile（若存在且非内置项）。

        内置项选中时不写回（编辑区内容是上次载入的内置占位，不应污染用户列表）。
        草稿态（点了「＋新建」但未保存）不写回：草稿不归属任何 profile，未保存即
        apply/切换视为放弃草稿，避免静默以下拉选中项名字覆盖已有配置。
        """
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
        # 不存在则追加（编辑区新建但未点「保存为新配置」的场景兜底）。
        self._profiles.append(profile)

    def _load_profile_to_edit(self, profile: TranslationProfile) -> None:
        self._name_edit.setText(profile.name)
        self._base_url_edit.setText(profile.base_url)
        self._model_edit.setText(profile.model)
        self._api_key_edit.setText(profile.api_key or "")
        self._temperature_spin.setValue(float(profile.temperature))

    def _clear_edit(self) -> None:
        self._name_edit.clear()
        self._base_url_edit.clear()
        self._model_edit.clear()
        self._api_key_edit.clear()
        self._temperature_spin.setValue(0.3)

    def _set_edit_enabled(self, enabled: bool) -> None:
        """整体启用/禁用编辑区字段控件（不含按钮）。"""
        for w in (
            self._name_edit,
            self._base_url_edit,
            self._model_edit,
            self._api_key_edit,
            self._temperature_spin,
        ):
            w.setEnabled(enabled)

    def _activate_user_edit_mode(self) -> None:
        """切换到用户配置编辑态：隐藏内置提示、启用编辑区与「保存为新配置」。"""
        self._builtin_hint_label.hide()
        self._set_edit_enabled(True)
        self._save_new_btn.setEnabled(True)

    def _sync_delete_profile_btn(self) -> None:
        """按下拉当前选中项同步「删除」可用态（仅用户自建配置可删）。"""
        name = self._profile_combo.currentText()
        self._delete_profile_btn.setEnabled(
            name not in _BUILTIN_NAMES and any(p.name == name for p in self._profiles)
        )

    def _profile_from_edit(self, name: str | None = None) -> TranslationProfile:
        """从编辑区控件构造用户 LLM profile。

        保存/写回的 profile 固定 backend_type="openai_compatible"（编辑区仅服务用户 LLM 配置）。
        """
        return TranslationProfile(
            name=(name if name is not None else self._name_edit.text().strip()),
            base_url=self._base_url_edit.text().strip(),
            model=self._model_edit.text().strip(),
            api_key=self._api_key_edit.text() or None,
            temperature=float(self._temperature_spin.value()),
            timeout=60.0,
            backend_type="openai_compatible",
        )

    @staticmethod
    def _builtin_profile(name: str) -> TranslationProfile:
        """按 name 取内置项对象（调用方已确保 name 在内置集合中）。"""
        for p in _BUILTIN_PROFILES:
            if p.name == name:
                return p
        # 不应到达：调用前已用 _BUILTIN_NAMES 校验。
        return _BUILTIN_PROFILES[0]

    def _current_test_profile(self) -> TranslationProfile | None:
        """构造「测试连接」使用的 profile，与编辑区选中态匹配。

        - 内置项选中：直接返回对应内置 profile 对象。
        - 用户项选中：从编辑区构造；字段缺失时写错误提示并返回 None。
        """
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
        """把 result_ui 值同步到 ComboBox；非法值回退到 popup。"""
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
