# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DictaShift.

发布形态（主会话决策）：
- one-dir（COLLECT，目录形态，启动比 one-file 快）。
- windowed（console=False，无控制台窗口，桌面常驻应用）。
- 不含模型文件（assets/models）：模型走 GitHub Releases 应用内下载，
  首次运行在「设置 - 语音识别」页下载到 %APPDATA%\\DictaShift\\models。

本 spec 通过 PyInstaller.utils.hooks.collect_all 收集以下含动态库/数据
的包，逐个说明原因：

- onnxruntime：推理引擎在 onnxruntime\\capi 下携带 onnxruntime.dll 等
  原生动态库与 pyd，必须整体打入（常见坑：漏掉 capi 的 dll 导致 import
  报 DLL 加载失败）。collect_all 会同时收 binaries/datas/hiddenimports。
- sentencepiece：Python wheel 带 sentencepiece/*.pyd（C++ 扩展），需整体
  收集避免翻译分词器加载失败。
- sounddevice：依赖 _sounddevice_data/PortAudio 的预编译 dll，collect_all
  收其 datas（PortAudio 二进制）+ binaries。
- PySide6：Qt 的 plugins/translations/依赖 dll，collect_all 确保 Qt 插件
  （平台插件 qwindows、音频等）齐全；否则窗口/托盘无法创建。
- numpy：sounddevice/onnxruntime 运行期依赖，整体收集保证 ABI 一致。

不收集：assets/models（模型走下载）、tests（无运行期价值）。
assets/icons（应用图标）按下文显式加入 datas，运行时经
dictashift.resources.resource_path 解析（_internal/assets/icons）。
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 仓库内图标资源（spec 位于 scripts/ 下，SPECPATH 即 scripts 目录）。
_ICONS_SRC = os.path.join(SPECPATH, "..", "assets", "icons")
_ICON_ICO = os.path.join(_ICONS_SRC, "dictashift.ico")

# 逐包收集，返回 (datas, binaries, hiddenimports) 三元组。
datas = []
binaries = []
hiddenimports = []

for pkg, reason in [
    ("onnxruntime", "ONNX 推理引擎原生 dll（onnxruntime.dll 等）"),
    ("sentencepiece", "BPE 分词器 C++ 扩展 pyd"),
    ("sounddevice", "PortAudio 预编译 dll (_sounddevice_data)"),
    # PySide6 / numpy 不用 collect_all：PyInstaller 内置 hook 会按实际 import
    # 收集所需 Qt 依赖；collect_all 会把 QML/SQL/WebEngine 等未用模块整体打入
    # （体积从 ~750MB 膨胀的主要来源），配合下方 excludes 排除未用 Qt 模块。
]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pynput 的 win32 后端按平台动态 import，作为 hiddenimports 兜底。
hiddenimports += collect_submodules("pynput") + collect_submodules("pynput.win32")

# 应用图标（assets/icons -> _internal/assets/icons）：托盘/窗口/设置页 logo 运行时加载。
# 图标由 scripts/gen_icon.py 生成，需在打包前运行一次。
if os.path.isdir(_ICONS_SRC):
    datas += [(_ICONS_SRC, "assets/icons")]

# 未使用的 Qt 模块（GUI 只用 Widgets/Gui/Core + 托盘/输入法相关插件）。
_QT_UNUSED = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtSql", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtSerialPort",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtTest",
    "PySide6.QtNetwork",
]

block_cipher = None

a = Analysis(
    # 不能直接打 src/dictashift/main.py：它内部用相对导入
    # (from .application import ...)，作为顶层脚本入口会失败
    # (ImportError: attempted relative import with no known parent package)。
    # 改用顶层启动器 scripts/launch.py，它以包形式导入 dictashift.main:main。
    # 注意：PyInstaller 相对 spec 文件所在目录解析脚本路径，所以写 launch.py。
    ["launch.py"],
    pathex=["src", "scripts"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # tkinter 未使用，减小体积。
        "tkinter",
        # 测试代码不打包。
        "tests",
        "pytest",
    ] + _QT_UNUSED,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DictaShift",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed：无控制台窗口。
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 应用图标（多尺寸 ico，由 scripts/gen_icon.py 生成）。
    icon=_ICON_ICO if os.path.isfile(_ICON_ICO) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DictaShift",
)
