"""设置窗口（四页 QTabWidget）。

页面结构（按 计划.md / 任务规格）：
1. 常规：启用语音输入、开机启动(仅保存设置)、显示状态悬浮窗、全局禁用快捷键。
2. 快捷键：中文输入(f8)、中文→英文(f9)、取消当前任务(esc)。
3. 语音识别：模型(只读)、推理设备、GPU 设备、实际运行设备(只读)、
   麦克风、最长录音秒数、重新检测硬件、测试识别(禁用占位)。
4. 翻译：当前配置 ComboBox、编辑区(base_url/model/api_key/temperature)、
   保存为新配置、测试连接(后台)。

线程模型：
- 控件值 <-> AppSettings 双向绑定（load_from_settings / apply_to_settings）。
- 重后台工作（ASR initialize、翻译测试连接）通过 Qt signal 交给 application 层
  在后台线程执行，结果再 queued 回 UI；UI 线程不做网络/模型工作。
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
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..asr.providers import AsrDevice
from ..audio.devices import list_input_devices
from ..settings import AppSettings
from ..translation import TranslationProfile
from .hotkey_editor import HotkeyEdit


# 设备 ComboBox 显示文案与 AsrDevice 值的双向映射。
_DEVICE_LABELS: list[tuple[str, AsrDevice]] = [
    ("自动选择", AsrDevice.AUTO),
    ("CPU", AsrDevice.CPU),
    ("NVIDIA CUDA", AsrDevice.CUDA),
]


class SettingsWindow(QWidget):
    """设置窗口。

    信号（application 层连接）：
    - applied(settings)：用户点击「应用」或「确定」时发出，携带最新设置副本。
    - recheck_hardware_requested(device, device_id)：用户点击「重新检测硬件」。
    - test_connection_requested(profile)：用户点击「测试连接」（后台执行）。
    """

    applied = Signal(AppSettings)
    recheck_hardware_requested = Signal(object, int)  # AsrDevice, device_id
    test_connection_requested = Signal(object)  # TranslationProfile

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

    # ==================== 常规页 ====================

    def _build_general_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self._enable_check = QCheckBox("启用语音输入", page)
        form.addRow(self._enable_check)

        self._autostart_check = QCheckBox("开机启动（将在后续版本生效）", page)
        form.addRow(self._autostart_check)

        self._overlay_check = QCheckBox("显示状态悬浮窗", page)
        form.addRow(self._overlay_check)

        form.addRow(QLabel("全局禁用快捷键：", page))
        self._toggle_hotkey = HotkeyEdit(page)
        form.addRow(self._toggle_hotkey)

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

        form.addRow("模型：", QLabel("SenseVoiceSmall ONNX", page))

        self._device_combo = QComboBox(page)
        for label, _dev in _DEVICE_LABELS:
            self._device_combo.addItem(label)
        form.addRow("推理设备：", self._device_combo)

        self._gpu_spin = QSpinBox(page)
        self._gpu_spin.setRange(0, 7)
        self._gpu_spin.setValue(0)
        form.addRow("GPU 设备：", self._gpu_spin)

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

        # 内存中维护的 profiles 列表（编辑/保存时操作；apply 时同步到 settings）。
        self._profiles: list[TranslationProfile] = []

        return page

    # ==================== 双向绑定 ====================

    def load_from_settings(self, settings: AppSettings) -> None:
        """从 AppSettings 填充所有控件值，并缓存基底副本。"""
        import dataclasses
        self._last_loaded = dataclasses.replace(settings)
        # 常规
        self._enable_check.setChecked(settings.enabled)
        self._autostart_check.setChecked(settings.ui.launch_at_login)
        self._overlay_check.setChecked(settings.ui.show_overlay)
        self._toggle_hotkey.set_config_text(settings.hotkeys.toggle_enabled)
        # 快捷键
        self._dictate_zh_edit.set_config_text(settings.hotkeys.dictate_zh)
        self._dictate_en_edit.set_config_text(settings.hotkeys.dictate_en)
        self._cancel_edit.set_config_text(settings.hotkeys.cancel)
        # ASR
        self._select_device(settings.asr.device)
        self._gpu_spin.setValue(int(settings.asr.device_id))
        self._max_record_spin.setValue(int(settings.asr.max_record_seconds))
        self._select_mic(settings.asr.mic_device_index)
        # 翻译
        self._profiles = list(settings.translation.profiles)
        self._rebuild_profile_combo(settings.translation.active_profile)

    def apply_to_settings(self, settings: AppSettings) -> AppSettings:
        """把控件当前值写回 settings 并返回。

        在副本上修改，避免污染传入对象。
        """
        import dataclasses

        settings = dataclasses.replace(settings)
        settings.enabled = self._enable_check.isChecked()
        # UI 子配置
        ui = dataclasses.replace(
            settings.ui,
            launch_at_login=self._autostart_check.isChecked(),
            show_overlay=self._overlay_check.isChecked(),
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
            device=self._current_device().value,
            device_id=int(self._gpu_spin.value()),
            max_record_seconds=int(self._max_record_spin.value()),
            mic_device_index=self._current_mic_index(),
        )
        settings.asr = asr
        # 翻译：把编辑区当前内容写回当前选中的 profile，再同步列表/active。
        self._flush_edit_to_active_profile()
        tr = dataclasses.replace(
            settings.translation,
            profiles=list(self._profiles),
            active_profile=self._profile_combo.currentText(),
        )
        settings.translation = tr
        return settings

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

    # ==================== 按钮事件 ====================

    def _on_ok(self) -> None:
        new_settings = self.apply_to_settings(self._last_loaded)
        self.applied.emit(new_settings)
        self.hide()

    def _on_apply(self) -> None:
        new_settings = self.apply_to_settings(self._last_loaded)
        self.applied.emit(new_settings)

    def _on_recheck_clicked(self) -> None:
        device = self._current_device()
        device_id = int(self._gpu_spin.value())
        self._recheck_btn.setEnabled(False)
        self._recheck_btn.setText("加载中…")
        self.recheck_hardware_requested.emit(device, device_id)

    def _on_test_conn_clicked(self) -> None:
        profile = self._profile_from_edit()
        if not profile.base_url or not profile.model:
            self._test_result_label.setText("请填写 Base URL 和模型")
            return
        self._test_conn_btn.setEnabled(False)
        self._test_conn_btn.setText("测试中…")
        self._test_result_label.setText("测试中…")
        self.test_connection_requested.emit(profile)

    def _on_save_new_clicked(self) -> None:
        """把当前编辑区内容保存为一个新配置（追加到 profiles）。"""
        profile = self._profile_from_edit()
        if not profile.name.strip():
            self._test_result_label.setText("请填写配置名称")
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
        """ComboBox 选择变化时把对应 profile 载入编辑区。"""
        name = self._profile_combo.currentText()
        for p in self._profiles:
            if p.name == name:
                self._load_profile_to_edit(p)
                return
        # 空选或未匹配：清空编辑区
        self._clear_edit()

    def _rebuild_profile_combo(self, active_name: str) -> None:
        """根据 self._profiles 重建 ComboBox 并选中 active_name。"""
        was = self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for p in self._profiles:
            self._profile_combo.addItem(p.name)
        if active_name and any(p.name == active_name for p in self._profiles):
            self._profile_combo.setCurrentText(active_name)
        elif self._profiles:
            self._profile_combo.setCurrentIndex(0)
        self._profile_combo.blockSignals(was)
        # 触发一次编辑区载入
        self._on_profile_selected(self._profile_combo.currentIndex())

    def _flush_edit_to_active_profile(self) -> None:
        """把当前编辑区内容写回当前选中名的 profile（若存在）。"""
        name = self._profile_combo.currentText()
        if not name:
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

    def _profile_from_edit(self, name: str | None = None) -> TranslationProfile:
        """从编辑区控件构造 TranslationProfile。"""
        return TranslationProfile(
            name=(name if name is not None else self._name_edit.text().strip()),
            base_url=self._base_url_edit.text().strip(),
            model=self._model_edit.text().strip(),
            api_key=self._api_key_edit.text() or None,
            temperature=float(self._temperature_spin.value()),
            timeout=60.0,
        )

    # ==================== ASR 控件辅助 ====================

    def _current_device(self) -> AsrDevice:
        idx = self._device_combo.currentIndex()
        if 0 <= idx < len(_DEVICE_LABELS):
            return _DEVICE_LABELS[idx][1]
        return AsrDevice.AUTO

    def _select_device(self, device_str: str) -> None:
        try:
            dev = AsrDevice(device_str)
        except ValueError:
            dev = AsrDevice.AUTO
        for i, (_label, d) in enumerate(_DEVICE_LABELS):
            if d == dev:
                self._device_combo.setCurrentIndex(i)
                return
        self._device_combo.setCurrentIndex(0)

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

