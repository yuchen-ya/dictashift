"""重截 README 界面截图（docs/images/），可重复执行。

用法：``uv run python scripts/capture_screenshots.py``

注意：
- **不要**设置 QT_QPA_PLATFORM=offscreen（offscreen 渲染不了 CJK 字体，会变方框）；
  使用 Windows 原生平台真实渲染，窗口会短暂显示在屏幕上，属正常现象。
- 不触碰用户真实配置：设置窗口直接用内存中的 AppSettings demo 数据构造，
  不经过 SettingsRepository / %APPDATA%。

通用模式：QApplication → 构造控件 → 设置演示状态 → show() → QTest.qWait
→ widget.grab().save(png) → close。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGES_DIR = REPO_ROOT / "docs" / "images"

# 悬浮窗结果态示例文本（与旧图同款文案）。
_OVERLAY_RESULT_TEXT = "直输失败时的结果文本会直接替换状态文字，点击卡片任意处即可复制。"
# 右下角弹窗示例文本（与旧图同款文案）。
_POPUP_RESULT_TEXT = (
    "这是一次语音识别的结果文本。直输失败时会弹出此卡片，"
    "点击「复制」按钮将文本写入剪贴板。"
)
# 快速查词示例文本（中译英真实使用场景）。
_QUICK_LOOKUP_SOURCE = "请帮我把这段技术方案整理成简洁的英文摘要"
_QUICK_LOOKUP_RESULT = (
    "Please organize this technical proposal into a concise English summary."
)


def _grab(widget, name: str) -> None:
    """grab 当前控件并保存为 PNG 到 docs/images/。"""
    path = IMAGES_DIR / name
    pm = widget.grab()
    if pm.isNull():
        raise RuntimeError(f"grab 失败：{name}")
    pm.save(str(path), "PNG")
    print(f"[ok] {name}: {pm.width()}x{pm.height()}")


def _build_demo_settings():
    """构造演示用 AppSettings（纯内存，不落盘）。

    - 翻译页：一个用户 LLM 档案（本地 llama-server 预设）并置为活动档案，
      使编辑区有代表性内容。
    - 个性化页：启用句尾后缀「喵」+ 语气改写选中「傲娇」。
    """
    from dictashift.settings import AppSettings
    from dictashift.translation import TranslationProfile

    s = AppSettings()
    s.translation.profiles = [
        TranslationProfile(
            name="本地 llama-server",
            base_url="http://127.0.0.1:19120/v1",
            model="qwen2.5-7b-instruct",
        ),
    ]
    s.translation.active_profile = "本地 llama-server"
    s.personalize.suffix_enabled = True
    s.personalize.suffix_text = "喵"
    s.personalize.rewrite_style = "tsundere"
    return s


def capture_settings_tabs() -> None:
    """设置窗口 5 个 tab 各截一张。"""
    from dictashift.ui.settings_window import SettingsWindow

    win = SettingsWindow()
    win.load_from_settings(_build_demo_settings())
    win.show()
    QTest.qWait(300)
    names = [
        "settings-general.png",
        "settings-hotkeys.png",
        "settings-asr.png",
        "settings-translation.png",
        "settings-personalize.png",
    ]
    for index, name in enumerate(names):
        win._tabs.setCurrentIndex(index)
        QTest.qWait(150)
        _grab(win, name)
    win.close()


def capture_overlays() -> None:
    """悬浮窗 4 种状态各截一张（每种状态用全新窗口，避免状态互相污染）。"""
    from dictashift.core.state import AppState
    from dictashift.ui.overlay import StateOverlay

    states = [
        (AppState.PREPARING, "overlay-preparing.png"),
        (AppState.RECORDING, "overlay-recording.png"),
        (AppState.REWRITING, "overlay-rewriting.png"),
    ]
    for state, name in states:
        overlay = StateOverlay()
        overlay.set_state(state)
        overlay.show()
        QTest.qWait(200)
        _grab(overlay, name)
        overlay.close()

    overlay = StateOverlay()
    overlay.show_result(_OVERLAY_RESULT_TEXT)
    overlay.show()
    QTest.qWait(200)
    _grab(overlay, "overlay-result.png")
    overlay.close()


def capture_result_popup() -> None:
    """右下角结果弹窗（带示例文本）。"""
    from dictashift.ui.result_popup import ResultPopup

    popup = ResultPopup()
    popup.show_result(_POPUP_RESULT_TEXT)
    QTest.qWait(200)
    _grab(popup, "result-popup.png")
    popup.close()


def capture_quick_lookup() -> None:
    """快速查词居中浮窗（中译英完成态）。"""
    from dictashift.ui.quick_lookup_popup import QuickLookupPopup

    popup = QuickLookupPopup()
    popup.open_lookup(_QUICK_LOOKUP_SOURCE)
    popup._input.deselect()
    popup.show_result(
        popup.request_seq,
        _QUICK_LOOKUP_RESULT,
        actual_direction="zh2en",
    )
    QTest.qWait(250)
    _grab(popup, "quick-lookup.png")
    popup.close()


def main() -> int:
    from dictashift.ui.theme import app_stylesheet

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(app_stylesheet())
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    capture_settings_tabs()
    capture_overlays()
    capture_result_popup()
    capture_quick_lookup()
    app.quit()
    print("全部截图完成 ->", IMAGES_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
