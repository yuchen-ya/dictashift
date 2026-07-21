"""程序入口：QApplication + logging 配置 + DictaShiftApplication。

运行：uv run python -m dictashift
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .application import DictaShiftApplication


def _setup_logging() -> None:
    """配置顶层 logging：INFO 级别 + logs/dictashift.log FileHandler。

    日志目录在仓库根 logs/ 下，自动创建；创建失败时仅用 stderr 兜底。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "dictashift.log", encoding="utf-8"))
    except OSError:
        # 日志目录创建失败不应阻止启动；仅用 stderr。
        pass
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

    da = DictaShiftApplication(app)
    da.startup()

    code = app.exec()
    da.shutdown()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
