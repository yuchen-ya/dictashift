"""设置窗口（四页 QTabWidget）。

页面结构：
1. 常规：启用语音输入、开机启动(仅保存设置)、结果展示方式(右下角弹窗/常驻悬浮窗)、
   全局禁用快捷键、恢复默认设置按钮（重置常规/快捷键/语音识别/界面，不影响翻译配置）。
2. 快捷键：中文输入(f8)、中文→英文(f9)、取消当前任务(esc)。
3. 语音识别：模型(已安装/未安装 状态 + 下载按钮 + 进度条)、实际运行设备(只读)、
   麦克风、最长录音秒数、重新检测硬件、测试识别(禁用占位)。
   注：推理设备/GPU 设备控件已随 GPU 路线移除（设备固定为 AUTO/CPU，等价）。
4. 翻译：当前配置 ComboBox（内置免登录 Google/Bing + 用户 LLM profiles）、
   编辑区(base_url/model/api_key/temperature，仅作用于用户 LLM 配置)、
   保存为新配置、测试连接(后台)。

线程模型：
- 控件值 <-> AppSettings 双向绑定（load_from_settings / apply_to_settings）。
- 重后台工作（ASR initialize、翻译测试连接、模型下载）通过 Qt signal 交给
  application 层在后台线程执行，结果再 queued 回 UI；UI 线程不做网络/模型工作。
- 「开机启动」勾选状态直接读写注册表（is_launch_at_login_enabled），与
  settings.ui.launch_at_login 解耦（后者仅作记录）；apply 时调用
  set_launch_at_login 真正写注册表，失败弹 QMessageBox 警告。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
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
    UiSettings,
)
from ..translation import TranslationProfile, builtin_free_profiles, builtin_profile_names
from .hotkey_editor import HotkeyEdit


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
    """

    applied = Signal(AppSettings)
    recheck_hardware_requested = Signal()
    test_connection_requested = Signal(object)  # TranslationProfile
    model_download_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DictaShift 设置")
        self.resize(520, 460)

        # 最近一次 load_from_settings 缓存的 settings 副本，供 ok/apply 作为基底。
        self._last_loaded: AppSettings = AppSettings()

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_general_tab(), "常规")
        self._tabs.addTab(self._build_hotkeys_tab(), "快捷键")
        self._tabs.addTab(self._build_asr_tab(), "语音识别")
        self._tabs.addTab(self._build_translation_tab(), "翻译")

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
        root.addWidget(self._tabs, stretch=1)
        root.addLayout(btn_row)

        self._ok_btn.clicked.connect(self._on_ok)
        self._apply_btn.clicked.connect(self._on_apply)
        self._cancel_btn.clicked.connect(self.hide)

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
        form.addRow("结果展示方式：", self._result_ui_combo)

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

        top_form = QFormLayout()
        self._profile_combo = QComboBox(page)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        top_form.addRow("当前翻译配置：", self._profile_combo)
        outer.addLayout(top_form)

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

        # 内存中维护的用户 LLM profiles 列表（编辑/保存时操作；apply 时同步到 settings）。
        # 内置免登录项不在此列表中，由 _rebuild_profile_combo 拼接到下拉顶部。
        self._profiles: list[TranslationProfile] = []

        return page

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
        # ASR
        self._max_record_spin.setValue(int(settings.asr.max_record_seconds))
        self._select_mic(settings.asr.mic_device_index)
        # 模型状态刷新一次。
        self.refresh_model_status()
        # 翻译
        self._profiles = list(settings.translation.profiles)
        self._rebuild_profile_combo(settings.translation.active_profile)

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
        return settings

    # ==================== 一键重置 ====================

    def reset_to_defaults(self) -> None:
        """把非翻译页控件重置为 dataclass 默认值（仅改 UI，不持久化）。

        重置范围：常规(enabled=True + UiSettings 默认)、快捷键(HotkeySettings 默认)、
        语音识别(AsrSettings 默认)。翻译页内容（profiles / active / 编辑区）完全不动。
        用户仍需点「应用/确定」才落盘，与现有编辑行为一致。
        """
        default_ui = UiSettings()
        default_hk = HotkeySettings()
        default_asr = AsrSettings()
        # 常规
        self._enable_check.setChecked(True)
        self._autostart_check.setChecked(default_ui.launch_at_login)
        self._set_result_ui(default_ui.result_ui)
        self._toggle_hotkey.set_config_text(default_hk.toggle_enabled)
        # 快捷键
        self._dictate_zh_edit.set_config_text(default_hk.dictate_zh)
        self._dictate_en_edit.set_config_text(default_hk.dictate_en)
        self._cancel_edit.set_config_text(default_hk.cancel)
        # ASR
        self._max_record_spin.setValue(int(default_asr.max_record_seconds))
        self._select_mic(default_asr.mic_device_index)
        # 状态提示
        self._reset_status_label.setText("已恢复默认值，点击应用生效")

    def _on_reset_clicked(self) -> None:
        btn = QMessageBox.question(
            self,
            "恢复默认设置",
            "将恢复常规、快捷键、语音识别、界面设置为默认值，翻译配置不受影响。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if btn == QMessageBox.StandardButton.Yes:
            self.reset_to_defaults()

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

    # ==================== 翻译 profiles 编辑辅助 ====================

    def _on_profile_selected(self, _index: int) -> None:
        """ComboBox 选择变化时载入对应 profile 到编辑区，并切换编辑区启用态。

        - 内置项：编辑区整体禁用、显示提示标签、「保存为新配置」禁用。
        - 用户项：编辑区启用、隐藏提示标签、「保存为新配置」启用。
        """
        name = self._profile_combo.currentText()
        if name in _BUILTIN_NAMES:
            self._builtin_hint_label.show()
            self._set_edit_enabled(False)
            self._save_new_btn.setEnabled(False)
            self._load_profile_to_edit(self._builtin_profile(name))
            return
        self._builtin_hint_label.hide()
        self._set_edit_enabled(True)
        self._save_new_btn.setEnabled(True)
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
        """
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
