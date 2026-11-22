"""程序入口：QApplication + logging 配置 + DictaShiftApplication。

运行：uv run python -m dictashift
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .application import DictaShiftApplication
from .resources import load_app_icon


def _default_log_dir() -> Path:
    """日志目录：%APPDATA%\\DictaShift\\logs（无 APPDATA 回退 ~/.dictashift/logs）。

    开源安全审查 M2：不使用相对 cwd 的 logs/（打包版 cwd 不可预测，
    且日志可能含敏感诊断信息，应落在用户数据目录）。
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "DictaShift" / "logs"
    return Path.home() / ".dictashift" / "logs"


def _setup_logging() -> None:
    """配置顶层 logging：INFO 级别 + FileHandler。

    日志目录自动创建；创建失败时仅用 stderr 兜底。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        log_dir = _default_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "dictashift.log", encoding="utf-8"))
    except OSError:
        # 日志目录创建失败不应阻止启动；仅用 stderr。
        pass
    # httpx logs complete URLs at INFO, including Google Translate's source text.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> int:
    """程序主入口。"""
    _setup_logging()

    app = QApplication(sys.argv)
    app.setApplicationName("DictaShift")
    app.setOrganizationName("DictaShift")
    # 避免在最后一个窗口关闭时退出（托盘常驻）。
    app.setQuitOnLastWindowClosed(False)
    # 统一窗口图标（标题栏 + 任务栏）；图标文件缺失时保持 Qt 默认，不阻塞启动。
    icon = load_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    da = DictaShiftApplication(app)
    da.startup()

    code = app.exec()
    da.shutdown()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
