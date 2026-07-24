"""PyInstaller 打包入口启动器。

`src/dictashift/main.py` 内部使用了相对导入（`from .application import ...`），
直接作为打包脚本入口时，PyInstaller 会把它编译成无父包的顶层模块，相对导入
失败（`ImportError: attempted relative import with no known parent package`）。

本启动器是一个顶层脚本：确保 `dictashift` 包被作为包导入（而不是顶层脚本），
再调用其真正的入口 `dictashift.main:main`。仅用于打包，不影响源码运行。

源码运行请使用：uv run python -m dictashift。
"""

from __future__ import annotations

import sys

from dictashift.main import main

if __name__ == "__main__":
    sys.exit(main())
