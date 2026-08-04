"""应用装配层：把 controller / services / UI 粘合在一起。

职责 (AGENTS.md 5.1 / 5.3 / 5.5 / 11)：
- 持有 QApplication、SettingsRepository、AppSettings、ASRService、AudioRecorder、
  DispatcherBackend、ApplicationController、HotkeyService、TrayIcon、Overlay、
  SettingsWindow、ResultPopup、SelectionService、SelectionPopup。
- ControllerCallbacks 在 worker/pynput 线程触发，必须用 Qt queued signal 桥接到
  主线程；本层用内部 _Bridge QObject 定义 signals，回调 emit 这些 signal，
  并以 Qt.QueuedConnection 连接到主线程槽。
- 模型加载/网络请求/阻塞等待一律放后台线程；完成后 queued 通知 UI。
- 不修改 controller；当前任务 action 在本层自行跟踪（on_press 记录，
  recorder on_auto_stop 时据此调 handle_hotkey_release）。
- pynput 回调（_on_hotkey_press / _on_hotkey_release / _on_tray_toggle）只做
  最小工作：更新 _current_action + 把事件入 FIFO 队列后立即返回。controller 调用
  （含 recorder.start/stop 的 PortAudio 阻塞）由单线程消费线程按序执行，
  避免 pynput 监听线程被阻塞导致按键不跟手、松开事件延迟处理。
- 划词翻译：热键 action selection_translate 在本层直接路由到
  SelectionService.trigger_manual()（单次触发语义），不进入 controller 听写流水线；
  SelectionService 消费线程取词后经 _Bridge queued signal 驱动 SelectionPopup，
  翻译（网络阻塞）在一次性后台线程执行，不进 UI 线程也不占用取词消费线程。
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import shutil
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
from .personalize import build_personalize_processor
from .selection import CaptureResult, CaptureStatus, SelectionService
from .settings import AppSettings, SettingsRepository
from .settings.repository import app_data_dir
from .translation import DispatcherBackend, TranslationProfile, builtin_free_profiles
from .translation.langdetect import detect_direction
from .ui.overlay import StateOverlay
from .ui.result_popup import ResultPopup
from .ui.selection_popup import SelectionPopup
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
    model_progress = Signal(int)       # 模型下载进度百分比（<0 表示总大小未知）
    model_download_done = Signal(bool, str)  # 模型下载完成（ok, message）
    selection_loading = Signal()       # 划词取词成功，翻译进行中
    selection_result = Signal(str)     # 划词翻译译文
    selection_error = Signal(str)      # 划词翻译错误提示（简短脱敏）


# ==================== action 名称 <-> settings hotkeys 字段 ====================

# controller 约定的 action 名 <-> AppSettings.hotkeys 字段名。
# selection_translate 不进 controller：由 application 层直接路由到 SelectionService。
_ACTION_HOTKEY_FIELD = {
    "dictate_zh": "dictate_zh",
    "dictate_en": "dictate_en",
    "toggle_enabled": "toggle_enabled",
    "cancel": "cancel",
    "selection_translate": "selection_translate",
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
            on_first_frame=self._on_recorder_first_frame,
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
            wait_first_frame=True,
        )

        # ---- 当前任务 action 跟踪（application 层负责，不修改 controller）----
        self._current_action: str | None = None
        self._action_lock = threading.Lock()

        # ---- pynput 回调异步串行化：回调只入队，消费线程按 FIFO 调 controller ----
        # 解决根因：pynput 监听线程串行处理键事件，若在回调里同步调用
        # controller.handle_hotkey_press/release（含 recorder.start/stop 的
        # PortAudio 阻塞），会导致后续键事件排队 -> 按键不跟手、松开事件延迟
        # 处理 -> 松手后仍在录音 -> 录音尾巴长 -> ASR 更慢。
        self._hotkey_queue: queue.Queue = queue.Queue()
        self._hotkey_exec_thread = threading.Thread(
            target=self._hotkey_consumer_loop,
            name="dictashift-hotkey-exec",
            daemon=True,
        )
        self._hotkey_exec_thread.start()

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

        # ---- 划词翻译：取词服务 + 悬浮卡片 ----
        # 悬浮卡片在主线程创建，经 queued signal 驱动（show_loading/show_result/show_error）。
        self._selection_popup = SelectionPopup(
            auto_close_seconds=self.settings.selection_translate.popup_timeout_s
        )
        # 取词服务：pynput 鼠标监听 -> FIFO 队列 -> 单消费线程取词 ->
        # on_result（消费线程）在本层分支后 queued 到主线程。
        self._selection = SelectionService(on_result=self._on_selection_result)
        self._selection.set_auto_trigger(self.settings.selection_translate.auto_trigger)
        # 服务运行状态（start/stop 由 _apply_selection_settings 统一管理）。
        self._selection_started = False
        # 划词翻译请求序号：新触发递增，过期翻译结果丢弃（防止旧译文覆盖新弹窗）。
        self._selection_seq = 0
        self._selection_seq_lock = threading.Lock()

        # ---- ASR 初始化/重检测/翻译测试 的线程引用（daemon，不 join）----
        self._asr_init_thread: threading.Thread | None = None

        # 「清除全部数据并退出」流程标记：True 时 shutdown 跳过 repo.save（避免重建
        # settings.json）。
        self._data_cleared: bool = False

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
        self._bridge.model_progress.connect(
            self._settings_window.set_model_download_progress, Qt.QueuedConnection
        )
        self._bridge.model_download_done.connect(
            self._on_model_download_done, Qt.QueuedConnection
        )
        # 划词翻译：取词/翻译线程 -> 主线程悬浮卡片。
        self._bridge.selection_loading.connect(
            self._on_selection_loading, Qt.QueuedConnection
        )
        self._bridge.selection_result.connect(
            self._on_selection_result_ready, Qt.QueuedConnection
        )
        self._bridge.selection_error.connect(
            self._on_selection_error_ready, Qt.QueuedConnection
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
        self._settings_window.model_download_requested.connect(
            self._on_model_download_requested, Qt.QueuedConnection
        )
        self._settings_window.test_popup_requested.connect(
            self._on_test_popup, Qt.QueuedConnection
        )
        self._settings_window.clear_data_requested.connect(
            self._on_clear_data, Qt.QueuedConnection
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
        # 划词翻译：按配置决定是否启动取词服务（默认关闭）。
        self._apply_selection_settings()
        # 初始化翻译 profiles：内置免登录项 + 用户 LLM profiles
        self._controller.set_translation_profiles(
            self._translation_profiles_for_controller(), self.settings.translation.active_profile
        )
        # 初始化个性化后处理器（语气改写/句尾后缀，复用翻译活动档案的 LLM 配置）
        self._controller.set_personalize_processor(
            build_personalize_processor(self.settings)
        )
        # 后台初始化 ASR（不阻塞 UI）。设备固定 AUTO/0（GPU 路线已移除）。
        self._start_asr_init_thread(AsrDevice.AUTO, 0)

    def shutdown(self) -> None:
        """退出收尾：停 hotkey、停划词监听、停录音、释放 ASR、保存设置。"""
        try:
            self._hotkeys.stop()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] hotkey stop 异常")
        try:
            self._stop_selection_service()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] selection stop 异常")
        # 投递哨兵并等待消费线程处理完排队事件（尤其 release），保证在
        # controller shutdown 前已结束录音/启动 worker；2s 超时兜底避免卡死。
        try:
            self._hotkey_queue.put(None)
            self._hotkey_exec_thread.join(timeout=2.0)
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] hotkey exec 线程 join 异常")
        try:
            self._controller.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] controller shutdown 异常")
        try:
            self._asr.release()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] asr release 异常")
        # 若已「清除全部数据并退出」则跳过保存设置（否则会重建 settings.json
        # 与父目录，与已删除的数据目录冲突）。
        if not getattr(self, "_data_cleared", False):
            try:
                self._repo.save(self.settings)
            except Exception:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] 保存设置失败")

    # ==================== 清除全部数据并退出 ====================

    def _close_logging_handlers(self) -> None:
        """关闭并移除 root logger 的所有 handler（释放日志文件句柄）。

        Windows 下文件句柄不关闭会导致 shutil.rmtree 失败（ONNX session 内存映射的
        模型文件已由 asr.release() 释放；日志文件句柄在此释放）。
        """
        root = logging.getLogger()
        for handler in list(root.handlers):
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                root.removeHandler(handler)

    def _on_clear_data(self) -> None:
        """「清除全部数据并退出」：停服务 -> 关日志句柄 -> 删数据目录 -> 标记 -> quit。

        顺序很重要：
        1. 先停 hotkey / selection / controller.shutdown / asr.release（ONNX session
           可能内存映射模型文件，必须先 release 否则 Windows 下删不掉模型）；
        2. 再关闭日志文件句柄（rmtree 才能删除 logs/dictashift.log）；
        3. shutil.rmtree(ignore_errors=True) 删除整个应用数据目录；
        4. 置 _data_cleared=True 防止 shutdown() 重建 settings.json；
        5. app.quit() 退出（触发主循环退出 -> main 调 da.shutdown()，因 _data_cleared
           跳过 repo.save）。

        删除失败不阻塞退出（ignore_errors=True + warning 日志）。
        """
        try:
            self._hotkeys.stop()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] clear_data: hotkey stop 异常")
        try:
            self._stop_selection_service()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] clear_data: selection stop 异常")
        try:
            self._controller.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] clear_data: controller shutdown 异常")
        try:
            self._asr.release()
        except Exception:  # noqa: BLE001
            logger.exception("[DictaShiftApplication] clear_data: asr release 异常")
        self._close_logging_handlers()
        try:
            shutil.rmtree(app_data_dir(), ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[DictaShiftApplication] clear_data: 删除数据目录失败（已忽略）: %s",
                app_data_dir(),
            )
        self._data_cleared = True
        self._app.quit()

    # ==================== ASR 后台初始化 / 重检测 ====================

    def _start_asr_init_thread(self, device: AsrDevice, device_id: int) -> None:
        """在后台线程执行 asr_service.initialize；完成后 emit asr_ready。

        AGENTS.md 5.1 / 11：initialize 同步且耗时，绝不在 UI 线程。
        若失败原因是模型文件缺失，给出专门的下载引导提示。
        """

        def _worker() -> None:
            try:
                self._asr.initialize(device, device_id=device_id)
            except Exception:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] ASR initialize 失败")
                # 模型文件缺失 -> 专门提示去设置页下载。
                from .asr.providers import default_model_dir, models_ready

                if not models_ready(default_model_dir()):
                    msg = "语音识别模型未安装，请在设置-语音识别页下载模型"
                else:
                    msg = "语音识别引擎初始化失败，请检查硬件或重试"
                # 失败通过 on_error 提示，但不阻止应用运行。
                self._bridge.error.emit(msg)
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
        # 按 result_ui 路由：
        #   "overlay" -> 直输成功（pasted=True）不打扰用户（悬浮窗不显示结果）；
        #                直输失败（pasted=False）在悬浮窗显示结果文本。
        #   "popup"   -> 直输成功（pasted=True）不打扰用户，直接 return；
        #                直输失败（无聚焦控件或 UIPI 拦截）弹右下角 ResultPopup。
        # AGENTS.md 5.5：输出失败必须给可操作兜底，且不默认覆盖剪贴板。
        logger.info(
            "[DictaShiftApplication] on_text_ready: pasted=%s, result_ui=%s, 文本长度=%d",
            pasted, self.settings.ui.result_ui, len(text),
        )
        if self.settings.ui.result_ui == "overlay":
            if pasted:
                return
            self._overlay.show_result(text, failed=True)
            return
        # popup 模式：成功不打扰，失败弹窗展示文本。
        if pasted:
            return
        self._result_popup.show_result(text)

    def _on_error(self, message: str) -> None:
        self._tray.show_message("DictaShift", message)
        self._overlay.set_state(AppState.ERROR)
        # 按 result_ui 路由可见的错误展示：overlay 在悬浮窗显示；
        # 否则弹 ResultPopup.show_error（标题「出错了」，可复制错误文本）。
        if self.settings.ui.result_ui == "overlay":
            self._overlay.show_error(message)
        else:
            self._result_popup.show_error(message)

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

    # ==================== pynput 回调异步串行化 ====================

    def _hotkey_consumer_loop(self) -> None:
        """单线程 FIFO 消费 (kind, action) 事件，串行调用 controller。

        kind == "press"  -> controller.handle_hotkey_press(action)
        kind == "release"-> controller.handle_hotkey_release(action)
        哨兵 None 退出循环（shutdown 时投递）。

        controller 内部已捕获异常，这里再加一层 try/except 防止消费线程
        被意外打死导致后续热键事件全部失效。
        """
        while True:
            item = self._hotkey_queue.get()
            if item is None:
                # 哨兵：正常退出。
                return
            kind, action = item
            try:
                if kind == "press":
                    self._controller.handle_hotkey_press(action)
                elif kind == "release":
                    self._controller.handle_hotkey_release(action)
            except Exception:  # noqa: BLE001 - 消费线程不得被打死
                logger.exception(
                    "[DictaShiftApplication] hotkey consumer 处理事件异常 kind=%s action=%s",
                    kind, action,
                )

    # ==================== hotkey -> controller ====================

    def _on_hotkey_press(self, action: str) -> None:
        """pynput 监听线程调用。只做最小工作：记录 action + 入队，立即返回。

        controller 调用（含 recorder.start 的 PortAudio 阻塞）由消费线程
        按序执行，避免阻塞 pynput 监听线程导致按键不跟手。

        selection_translate 是单次触发语义：直接路由到 SelectionService
        （trigger_manual 仅入队，非阻塞），不进入 controller 听写流水线。
        """
        if action == "selection_translate":
            self._trigger_selection_translate()
            return
        with self._action_lock:
            if action in ("dictate_zh", "dictate_en"):
                self._current_action = action
        self._hotkey_queue.put(("press", action))

    def _on_hotkey_release(self, action: str) -> None:
        """pynput 监听线程调用。只做最小工作：清除 action + 入队，立即返回。

        松开事件及时入队（不阻塞），消费线程随后按序调 controller.handle_hotkey_release
        结束录音，避免松手后仍在录音。

        selection_translate 无按住/松开语义（press-only action，正常不会收到
        release；防御性忽略，绝不进入 controller）。
        """
        if action == "selection_translate":
            return
        with self._action_lock:
            if self._current_action == action:
                self._current_action = None
        self._hotkey_queue.put(("release", action))

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

    def _on_recorder_first_frame(self) -> None:
        """recorder 首个音频回调帧到达通知（PortAudio 线程）。

        与 _on_auto_stop / _on_stream_error 同模式：PortAudio 线程只做最小工作——
        起一次性后台线程调 controller._on_first_frame。不能在音频回调线程直接调
        controller（后者会取状态机锁、可能触发 _goto 的状态回调，且 _on_first_frame
        在 wait_first_frame=False 时根本不该被订阅；统一转后台线程保持回调线程轻量）。
        """
        def _handle_in_worker() -> None:
            try:
                self._controller._on_first_frame()
            except Exception:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] on_first_frame 异常")

        threading.Thread(
            target=_handle_in_worker, name="dictashift-first-frame", daemon=True
        ).start()

    # ==================== 托盘槽 ====================

    def _on_tray_toggle(self) -> None:
        """托盘「启用」勾选：模拟 toggle_enabled 按键（入队，避免阻塞主线程）。

        录音中 toggle 会让 controller discard（PortAudio 关流阻塞），若直接在
        UI 主线程调用会卡顿；统一走队列由消费线程执行。
        """
        self._hotkey_queue.put(("press", "toggle_enabled"))

    # ==================== 划词翻译 ====================

    def _trigger_selection_translate(self) -> None:
        """热键「划词翻译」入口（pynput 监听线程）：服务运行中才触发手动取词。

        trigger_manual 只是入队操作（非阻塞），符合 pynput 回调约束；
        功能未启用（服务未运行）时静默忽略。
        """
        if self._selection_started:
            self._selection.trigger_manual()

    def _on_selection_result(self, result: CaptureResult) -> None:
        """SelectionService 消费线程回调：按 CaptureStatus 分支。

        - success：queued 通知主线程 show_loading，随后一次性后台线程执行翻译
          （网络阻塞：绝不在 UI 线程——AGENTS.md 11；也不占用取词消费线程，
          避免一次慢翻译（最长 profile.timeout）阻塞后续取词）。
        - too_long：queued show_error 提示用户缩短选区。
        - no_text / skipped_self：静默（仅日志），不打扰用户。
        - error：queued show_error。取词已尽力还原剪贴板，但用户触发了功能却
          无任何反馈会像是「坏了」；与 _Bridge error 信号的可见提示风格一致，
          只是展示位置换成划词悬浮卡片（比托盘气泡更贴近触发点）。
        """
        if result.status is CaptureStatus.SUCCESS and result.text:
            self._bridge.selection_loading.emit()
            with self._selection_seq_lock:
                self._selection_seq += 1
                seq = self._selection_seq
            self._start_selection_translate(result.text, seq)
            return
        if result.status is CaptureStatus.TOO_LONG:
            self._bridge.selection_error.emit(result.reason or "选中文本过长，无法翻译")
            return
        if result.status is CaptureStatus.ERROR:
            reason = result.reason or "未知错误"
            self._bridge.selection_error.emit(f"取词失败：{reason}"[:120])
            return
        # no_text / skipped_self：静默，仅日志。
        logger.info(
            "[DictaShiftApplication] 划词取词未翻译: status=%s reason=%s",
            result.status.value, result.reason,
        )

    def _start_selection_translate(self, text: str, seq: int) -> None:
        """一次性后台线程执行划词翻译：detect_direction -> 方向化 profile -> translate。

        完成/失败后按 seq 校验是否仍是最新请求（用户可能已触发新取词），
        过期结果直接丢弃，避免旧译文覆盖新弹窗内容。
        """
        backend = self._backend

        def _worker() -> None:
            translated: str | None = None
            msg: str | None = None
            try:
                profile = self._controller.get_active_profile()
                if profile is None:
                    msg = "未配置翻译服务：请先在设置中选择翻译配置"
                else:
                    direction = detect_direction(text)
                    directed = dataclasses.replace(profile, direction=direction)
                    translated = backend.translate(text, directed)
                    if not translated or not translated.strip():
                        translated = None
                        msg = "翻译失败：空响应"
            except Exception as e:  # noqa: BLE001 - 统一归入错误提示，不向外抛
                logger.exception("[DictaShiftApplication] 划词翻译失败")
                # 不泄露 api_key；e 可能含 url，截断（与 _on_test_connection 同策略）。
                msg = f"翻译失败：{type(e).__name__}: {str(e)[:120]}"
            with self._selection_seq_lock:
                stale = seq != self._selection_seq
            if stale:
                return
            if translated is not None:
                self._bridge.selection_result.emit(translated)
            elif msg is not None:
                self._bridge.selection_error.emit(msg)

        threading.Thread(
            target=_worker, name="dictashift-selection-translate", daemon=True
        ).start()

    # ---- 划词悬浮卡片槽（主线程，经 queued signal 触发）----

    def _on_selection_loading(self) -> None:
        self._selection_popup.show_loading()

    def _on_selection_result_ready(self, text: str) -> None:
        self._selection_popup.show_result(text)

    def _on_selection_error_ready(self, message: str) -> None:
        self._selection_popup.show_error(message)

    # ---- 划词服务生命周期（随设置启停）----

    def _stop_selection_service(self) -> None:
        """停止取词服务（幂等）；shutdown/clear_data/设置关闭时调用。"""
        if self._selection_started:
            self._selection.stop()
            self._selection_started = False

    def _apply_selection_settings(self) -> None:
        """按当前 settings 应用划词翻译配置（startup 与 _on_settings_applied 共用）。

        - enabled：开 -> start（已由 _selection_started 保证不重复 start），
          关 -> stop；手动触发随服务停止而不可用。
        - auto_trigger：False 时鼠标划选不触发（service 内部丢弃事件，
          listener 与消费线程保持运行），手动快捷键触发不受影响。
        - popup_timeout_s：更新 SelectionPopup 自动关闭秒数。
        """
        cfg = self.settings.selection_translate
        self._selection_popup.set_auto_close_seconds(cfg.popup_timeout_s)
        self._selection.set_auto_trigger(cfg.auto_trigger)
        if cfg.enabled and not self._selection_started:
            try:
                self._selection.start()
                self._selection_started = True
            except Exception:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] 划词监听启动失败")
                self._tray.show_message("DictaShift", "划词翻译监听启动失败")
        elif not cfg.enabled:
            self._stop_selection_service()

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

        # 更新个性化后处理器（与翻译共用活动档案，随「应用/确定」生效）
        self._controller.set_personalize_processor(
            build_personalize_processor(self.settings)
        )

        # overlay 显隐：仅 result_ui=="overlay" 时显示，切换即时生效。
        self._overlay.set_visible_enabled(self.settings.ui.result_ui == "overlay")
        # ASR 设备不再可变（GPU 路线已移除）：设置应用时不触发设备切换。

        # 划词翻译：总开关启停 service、自动触发开关热更新、弹窗时长更新。
        self._apply_selection_settings()

    def _translation_profiles_for_controller(self) -> list[TranslationProfile]:
        """构造传给 controller 的 profiles 列表：内置免登录项在前，用户 LLM 在后。"""
        return list(builtin_free_profiles()) + list(self.settings.translation.profiles)

    def _on_recheck_hardware(self) -> None:
        """设置窗口「重新检测硬件」：后台重新 initialize（设备固定 AUTO/0）。"""
        self._settings_window.set_recheck_loading(True)
        self._start_asr_init_thread(AsrDevice.AUTO, 0)

    def _on_test_popup(self) -> None:
        """设置窗口「测试右下角弹窗」：用固定样例文本调 ResultPopup.show_result，
        让用户手动验证弹窗可见性与复制功能（诊断「弹窗不弹 / 文本无处可复制」）。

        ResultPopup 的文本区是 QTextEdit，直接显示原文（\\n 换行没问题）；
        注：SendInput 直输路径会把 \\n 替换为空格，但这里是直接显示，不受影响。
        """
        self._result_popup.show_result(
            "这是一条弹窗测试文本。\n如果你能看到这个弹窗，说明弹窗路径工作正常；"
            "点击「复制」按钮应把本段文本写入剪贴板。"
        )

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

    def _on_model_download_requested(self) -> None:
        """设置窗口「下载模型」：后台线程下载到 user_model_dir()。

        AGENTS.md 11：网络/解压在后台线程；进度经 queued signal 更新 UI。
        """
        from .asr.model_download import download_models
        from .asr.providers import user_model_dir

        dest = user_model_dir()

        def _progress(downloaded: int, total: int) -> None:
            if total <= 0:
                # 总大小未知。
                self._bridge.model_progress.emit(-1)
                return
            percent = int(downloaded * 100 / total)
            self._bridge.model_progress.emit(percent)

        def _worker() -> None:
            try:
                download_models(dest, progress_cb=_progress)
                self._bridge.model_download_done.emit(True, "")
            except Exception as e:  # noqa: BLE001
                logger.exception("[DictaShiftApplication] 模型下载失败")
                # 不泄露 URL/响应头；e 消息可能含本地路径，截断。
                msg = f"{type(e).__name__}: {str(e)[:160]}"
                self._bridge.model_download_done.emit(False, msg)

        threading.Thread(
            target=_worker, name="dictashift-model-download", daemon=True
        ).start()

    def _on_model_download_done(self, ok: bool, message: str) -> None:
        """模型下载完成（queued 主线程）：刷新 UI，成功则重新初始化 ASR。"""
        if ok:
            self._settings_window.on_model_download_complete()
            # 重新初始化 ASR（复用现有后台 recheck 路径）。
            self._settings_window.set_recheck_loading(True)
            self._start_asr_init_thread(AsrDevice.AUTO, 0)
        else:
            self._settings_window.on_model_download_failed(message)

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
