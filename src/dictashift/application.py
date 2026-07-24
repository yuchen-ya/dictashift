"""应用装配层：把 controller / services / UI 粘合在一起。

职责 (AGENTS.md 5.1 / 5.3 / 5.5 / 11)：
- 持有 QApplication、SettingsRepository、AppSettings、ASRService、AudioRecorder、
  DispatcherBackend、ApplicationController、HotkeyService、TrayIcon、Overlay、
  SettingsWindow、ResultPopup。
- ControllerCallbacks 在 worker/pynput 线程触发，必须用 Qt queued signal 桥接到
  主线程；本层用内部 _Bridge QObject 定义 signals，回调 emit 这些 signal，
  并以 Qt.QueuedConnection 连接到主线程槽。
- 模型加载/网络请求/阻塞等待一律放后台线程；完成后 queued 通知 UI。
- 不修改 controller；当前任务 action 在本层自行跟踪（on_press 记录，
  recorder on_auto_stop 时据此调 handle_hotkey_release）。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import QApplication

from .asr.providers import AsrDevice
from .asr.service import ASRService
from .audio.recorder import AudioRecorder
from .core import ApplicationController, AppState, ControllerCallbacks
from .hotkeys.matcher import HotkeyBinding, parse_hotkey
from .hotkeys.service import HotkeyService
from .output.typing import type_text_at_cursor
from .settings import AppSettings, SettingsRepository
from .translation import DispatcherBackend, TranslationProfile, builtin_free_profiles
from .ui.overlay import StateOverlay
from .ui.result_popup import ResultPopup
from .ui.settings_window import SettingsWindow
from .ui.tray import TrayIcon

logger = logging.getLogger(__name__)


# ==================== 回调 -> 主线程桥接 ====================


class _Bridge(QObject):
    """把 controller 回调（worker 线程）桥接到主线程槽的信号集合。

    这些 signal 在 worker/pynput 线程被 emit，Qt 自动把连接到它们的槽
    （通过 Qt.QueuedConnection）排队到主线程事件循环执行。
    """

    state_changed = Signal(str)        # AppState.value
    text_ready = Signal(str, bool)     # text, pasted
    error = Signal(str)                # 脱敏错误消息
    enabled_changed = Signal(bool)
    asr_ready = Signal()               # ASR 后台 initialize 完成（无论成败）
    test_conn_result = Signal(bool, str)  # 翻译测试连接结果（ok, message）


# ==================== action 名称 <-> settings hotkeys 字段 ====================

# controller 约定的 action 名 <-> AppSettings.hotkeys 字段名。
_ACTION_HOTKEY_FIELD = {
    "dictate_zh": "dictate_zh",
    "dictate_en": "dictate_en",
    "toggle_enabled": "toggle_enabled",
    "cancel": "cancel",
}


class DictaShiftApplication:
    """应用装配层。

    用法：
        app = QApplication(...)
        da = DictaShiftApplication(app)
        da.startup()
        app.exec()
        da.shutdown()
    """

    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._repo = SettingsRepository()
        self.settings: AppSettings = self._repo.load()

        # ---- 桥接 ----
        self._bridge = _Bridge()

        # ---- 服务（ASR 先不初始化，后台 initialize）----
        self._asr = ASRService()
        self._backend = DispatcherBackend()
        # recorder 的 on_auto_stop 在 PortAudio 线程触发；只做最小工作：
        # 触发 _on_auto_stop（通过 queued signal 转主线程，避免在音频线程回调 controller）。
        self._recorder = AudioRecorder(
            sample_rate=16000,
            max_seconds=self.settings.asr.max_record_seconds,
            device_index=self.settings.asr.mic_device_index,
            on_auto_stop=self._on_auto_stop,
            on_stream_error=self._on_stream_error,
        )

        callbacks = ControllerCallbacks(
            on_state_changed=self._bridge.state_changed.emit,
            on_text_ready=self._bridge.text_ready.emit,
            on_error=self._bridge.error.emit,
            on_enabled_changed=self._bridge.enabled_changed.emit,
        )
        self._controller = ApplicationController(
            asr_service=self._asr,
            recorder=self._recorder,
            translation_backend=self._backend,
            paste_func=type_text_at_cursor,
            callbacks=callbacks,
        )

        # ---- 当前任务 action 跟踪（application 层负责，不修改 controller）----
        self._current_action: str | None = None
        self._action_lock = threading.Lock()

        # ---- hotkey service（先构造，startup 时 start）----
        self._hotkeys = HotkeyService(
            bindings=self._bindings_from_settings(self.settings),
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
        )

        # ---- UI ----
        self._overlay = StateOverlay()
        self._tray = TrayIcon()
        self._settings_window = SettingsWindow()
        self._settings_window.load_from_settings(self.settings)
        # 直输失败的结果弹窗：主线程创建，经 queued signal 调用 show_result。
        self._result_popup = ResultPopup()

        # ---- ASR 初始化/重检测/翻译测试 的线程引用（daemon，不 join）----
        self._asr_init_thread: threading.Thread | None = None

        self._connect_signals()

    # ==================== 信号连接 ====================

    def _connect_signals(self) -> None:
        # 桥接 signal -> 主线程槽（Qt.QueuedConnection 保证跨线程安全）。
        self._bridge.state_changed.connect(self._on_state_changed, Qt.QueuedConnection)
        self._bridge.text_ready.connect(self._on_text_ready, Qt.QueuedConnection)
        self._bridge.error.connect(self._on_error, Qt.QueuedConnection)
        self._bridge.enabled_changed.connect(self._on_enabled_changed, Qt.QueuedConnection)
        self._bridge.asr_ready.connect(self._on_asr_ready, Qt.QueuedConnection)
        self._bridge.test_conn_result.connect(
            self._settings_window.set_test_connection_result, Qt.QueuedConnection
        )

        # 托盘
        self._tray.toggle_enabled_requested.connect(self._on_tray_toggle, Qt.QueuedConnection)
        self._tray.settings_requested.connect(self._open_settings, Qt.QueuedConnection)
        self._tray.quit_requested.connect(self._app.quit, Qt.QueuedConnection)

        # 设置窗口
        self._settings_window.applied.connect(self._on_settings_applied, Qt.QueuedConnection)
        self._settings_window.recheck_hardware_requested.connect(
            self._on_recheck_hardware, Qt.QueuedConnection
        )
        self._settings_window.test_connection_requested.connect(
            self._on_test_connection, Qt.QueuedConnection
        )

    # ==================== 生命周期 ====================

    def startup(self) -> None:
        """启动应用：controller.start + 托盘/overlay + hotkey + 后台 ASR 初始化。"""
        self._controller.start(enabled=self.settings.enabled)
        self._tray.show()
        self._tray.set_enabled(self.settings.enabled)
        # overlay 显隐由设置控制：仅 result_ui=="overlay" 时常驻显示。
        self._overlay.set_visible_enabled(self.settings.ui.result_ui == "overlay")
        self._overlay.set_state(self._controller.state)
        # 启动 hotkey 监听（pynput Listener 后台线程）
        try:
            self._hotkeys.start()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] 启动 hotkey 监听失败")
            self._tray.show_message("DictaShift", "全局快捷键监听启动失败")
        # 初始化翻译 profiles：内置免登录项 + 用户 LLM profiles
        self._controller.set_translation_profiles(
            self._translation_profiles_for_controller(), self.settings.translation.active_profile
        )
        # 后台初始化 ASR（不阻塞 UI）。设备固定 AUTO/0（GPU 路线已移除）。
        self._start_asr_init_thread(AsrDevice.AUTO, 0)

    def shutdown(self) -> None:
        """退出收尾：停 hotkey、停录音、释放 ASR、保存设置。"""
        try:
            self._hotkeys.stop()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] hotkey stop 异常")
        try:
            self._controller.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] controller shutdown 异常")
        try:
            self._asr.release()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] asr release 异常")
        try:
            self._repo.save(self.settings)
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] 保存设置失败")

    # ==================== ASR 后台初始化 / 重检测 ====================

    def _start_asr_init_thread(self, device: AsrDevice, device_id: int) -> None:
        """在后台线程执行 asr_service.initialize；完成后 emit asr_ready。

        AGENTS.md 5.1 / 11：initialize 同步且耗时，绝不在 UI 线程。
        """

        def _worker() -> None:
            try:
                self._asr.initialize(device, device_id=device_id)
            except Exception:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] ASR initialize 失败")
                # 失败通过 on_error 提示，但不阻止应用运行。
                self._bridge.error.emit("语音识别引擎初始化失败，请检查硬件或重试")
            finally:
                self._bridge.asr_ready.emit()

        self._asr_init_thread = threading.Thread(
            target=_worker, name="dictashift-asr-init", daemon=True
        )
        self._asr_init_thread.start()

    # ==================== 桥接槽（主线程）====================

    def _on_state_changed(self, state_value: str) -> None:
        try:
            state = AppState(state_value)
        except ValueError:
            return
        self._overlay.set_state(state)

    def _on_text_ready(self, text: str, pasted: bool) -> None:
        # pasted=True：直输成功，不打扰用户。
        # pasted=False：直输失败（无聚焦控件或 UIPI 拦截），按 result_ui 路由：
        #   "popup"  -> 右下角弹通知式卡片（平时无悬浮窗）；
        #   "overlay"-> 转译内容+复制按钮显示在常驻悬浮窗里（不弹 ResultPopup）。
        # AGENTS.md 5.5：输出失败必须给可操作兜底，且不默认覆盖剪贴板。
        if pasted:
            return
        if self.settings.ui.result_ui == "overlay":
            self._overlay.show_result(text)
        else:
            self._result_popup.show_result(text)

    def _on_error(self, message: str) -> None:
        self._tray.show_message("DictaShift", message)
        self._overlay.set_state(AppState.ERROR)

    def _on_enabled_changed(self, enabled: bool) -> None:
        self._tray.set_enabled(enabled)
        # 同步到内存 settings（保存时落盘）。
        self.settings.enabled = enabled

    def _on_asr_ready(self) -> None:
        """ASR 后台初始化/重检测完成：更新 UI 实际设备标签。"""
        self._settings_window.set_actual_device(
            self._asr.actual_device, self._asr.actual_providers
        )
        self._settings_window.set_recheck_loading(False)

    # ==================== hotkey -> controller ====================

    def _on_hotkey_press(self, action: str) -> None:
        """pynput 监听线程调用。记录当前 action，转发给 controller。"""
        with self._action_lock:
            if action in ("dictate_zh", "dictate_en"):
                self._current_action = action
        self._controller.handle_hotkey_press(action)

    def _on_hotkey_release(self, action: str) -> None:
        """pynput 监听线程调用。转发给 controller。"""
        with self._action_lock:
            if self._current_action == action:
                self._current_action = None
        self._controller.handle_hotkey_release(action)

    def _on_auto_stop(self) -> None:
        """recorder 达到最长录音时回调（PortAudio 线程）。

        只做最小工作：把结束事件转交一个一次性后台线程执行。
        注意不能投递到 UI 主线程——handle_hotkey_release 会同步调用
        recorder.stop()（PortAudio 关流是阻塞调用），主线程执行会违反
        AGENTS.md 11「UI 主线程不做阻塞等待」。controller 的回调经
        _bridge 的 QueuedConnection 回主线程，因此后台线程调用是安全的。
        """
        action = self._current_action
        if action is None:
            return

        def _release_in_worker() -> None:
            with self._action_lock:
                self._current_action = None
            self._controller.handle_hotkey_release(action)

        threading.Thread(
            target=_release_in_worker, name="dictashift-auto-stop", daemon=True
        ).start()

    def _on_stream_error(self, msg: str) -> None:
        """recorder 录音中途严重 status 回调（PortAudio 线程）。

        与 _on_auto_stop 同模式：PortAudio 线程只做最小工作——把事件转交一次性
        后台线程执行 controller._on_stream_error。后者会 discard（关流阻塞）+
        on_error + 回 IDLE，不能在音频回调线程内阻塞执行。
        """
        def _handle_in_worker() -> None:
            try:
                self._controller._on_stream_error(msg)
            except Exception:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] on_stream_error 异常")

        threading.Thread(
            target=_handle_in_worker, name="dictashift-stream-error", daemon=True
        ).start()

    # ==================== 托盘槽 ====================

    def _on_tray_toggle(self) -> None:
        """托盘「启用」勾选：模拟 toggle_enabled 按键。"""
        self._controller.handle_hotkey_press("toggle_enabled")

    # ==================== 设置窗口槽 ====================

    def _open_settings(self) -> None:
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_settings_applied(self, new_settings: AppSettings) -> None:
        """用户在设置窗口点击「应用」/「确定」。"""
        self.settings = new_settings

        # 保存
        try:
            self._repo.save(self.settings)
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] 保存设置失败")

        # 麦克风/最长录音变更应用到 recorder（录音中更换会抛 RuntimeError，
        # 此时设置窗口不可能在录音中打开应用，保守 try 包住）。
        try:
            self._recorder.device_index = self.settings.asr.mic_device_index
            self._recorder.max_seconds = self.settings.asr.max_record_seconds
        except RuntimeError:
            logger.warning("[DictaShiftApplication] 录音中，录音设备/时长变更将在下次生效")

        # 更新热键绑定（无需重启 listener）
        self._hotkeys.update_bindings(self._bindings_from_settings(self.settings))

        # 更新翻译 profiles：内置免登录项 + 用户 LLM profiles
        self._controller.set_translation_profiles(
            self._translation_profiles_for_controller(),
            self.settings.translation.active_profile,
        )

        # overlay 显隐：仅 result_ui=="overlay" 时显示，切换即时生效。
        self._overlay.set_visible_enabled(self.settings.ui.result_ui == "overlay")
        # ASR 设备不再可变（GPU 路线已移除）：设置应用时不触发设备切换。

    def _translation_profiles_for_controller(self) -> list[TranslationProfile]:
        """构造传给 controller 的 profiles 列表：内置免登录项在前，用户 LLM 在后。"""
        return list(builtin_free_profiles()) + list(self.settings.translation.profiles)

    def _on_recheck_hardware(self) -> None:
        """设置窗口「重新检测硬件」：后台重新 initialize（设备固定 AUTO/0）。"""
        self._settings_window.set_recheck_loading(True)
        self._start_asr_init_thread(AsrDevice.AUTO, 0)

    def _on_test_connection(self, profile: TranslationProfile) -> None:
        """设置窗口「测试连接」：后台线程调 backend.translate。

        AGENTS.md 11：网络请求不得在 UI 线程。
        """
        backend = self._backend

        def _worker() -> None:
            try:
                result = backend.translate("你好", profile)
                ok = True
                msg = result.strip()[:80] if result else "(空响应)"
            except Exception as e:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] 翻译测试连接失败")
                ok = False
                # 不泄露 api_key；e 可能含 url，截断。
                msg = f"{type(e).__name__}: {str(e)[:120]}"
            # queued 回主线程更新结果
            self._bridge.test_conn_result.emit(ok, msg)

        t = threading.Thread(target=_worker, name="dictashift-test-conn", daemon=True)
        t.start()

    # ==================== 工具：bindings 构造 ====================

    @staticmethod
    def _bindings_from_settings(settings: AppSettings) -> list[HotkeyBinding]:
        """从 AppSettings.hotkeys 构造 HotkeyBinding 列表。"""
        bindings: list[HotkeyBinding] = []
        for action, field in _ACTION_HOTKEY_FIELD.items():
            text = getattr(settings.hotkeys, field)
            try:
                keys = parse_hotkey(text)
            except ValueError:
                logger.warning(
                    "[DictaShiftApplication] 无效快捷键配置 action=%s text=%r，跳过",
                    action, text,
                )
                continue
            bindings.append(HotkeyBinding(action=action, keys=keys))
        return bindings
