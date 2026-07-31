"""程序化生成 DictaShift 应用图标。

设计：深色圆角方形底（暗色主题渐变）+ 白色麦克风图形 + 紫色声波弧线
（accent #8b5cf6，与 overlay/主题一致）。图形少而粗，保证 16px 下仍可辨识。

产物（assets/icons/）：
- dictashift.ico：含 16/24/32/48/64/128/256 多尺寸（PNG 压缩条目，Vista+ 支持）。
- dictashift.png：512x512，供设置窗口 header logo 等界面内使用。

用法（仓库根目录，可重复执行，覆盖同名产物）：
    uv run python scripts/gen_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# 生成脚本无需真实显示：offscreen 平台即可渲染 QImage。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

# ICO 内含尺寸；PNG 单独输出 512（界面 logo 用）。
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_PNG_SIZE = 512
# 母版渲染分辨率：全部图形按 1024x1024 坐标设计，缩放得到各尺寸（更平滑）。
_MASTER_SIZE = 1024

# 配色：沿用应用暗色主题 + 紫色 accent。
_BG_TOP = QColor("#2b2b31")
_BG_BOTTOM = QColor("#17171b")
_MIC = QColor("#f0f0f0")
_ACCENT = QColor("#8b5cf6")
_ACCENT_LIGHT = QColor("#a78bfa")


def _render_master() -> QImage:
    """在 1024x1024 画布上绘制图标母版（圆角方底 + 麦克风 + 声波弧线）。"""
    img = QImage(_MASTER_SIZE, _MASTER_SIZE, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    # ---- 圆角方形底：垂直暗色渐变（留 8px 内边距让圆角边缘抗锯齿到透明）----
    bg_path = QPainterPath()
    bg_path.addRoundedRect(QRectF(8, 8, 1008, 1008), 224, 224)
    grad = QLinearGradient(0, 0, 0, _MASTER_SIZE)
    grad.setColorAt(0.0, _BG_TOP)
    grad.setColorAt(1.0, _BG_BOTTOM)
    p.fillPath(bg_path, grad)

    # ---- 麦克风（白色，居中偏左，给右侧声波留位）----
    # 麦克风中轴 x=430；笔帽圆角让端点自然收圆。
    mic_pen = QPen(_MIC, 56, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    # 麦克头：胶囊形（圆角矩形，圆角半径=半宽 -> 两端全圆）。
    head_path = QPainterPath()
    head_path.addRoundedRect(QRectF(290, 130, 280, 420), 140, 140)
    p.fillPath(head_path, _MIC)
    # 托架弧线：包住麦克头下半圆（从 9 点钟方向经 6 点钟到 3 点钟方向）。
    p.setPen(mic_pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(200, 240, 460, 460), 180 * 16, 180 * 16)
    # 立杆 + 底座。
    p.drawLine(430, 700, 430, 800)
    p.drawLine(330, 830, 530, 830)

    # ---- 声波弧线（紫色 accent，右上两道）----
    wave_pen_inner = QPen(_ACCENT_LIGHT, 46, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    wave_pen_outer = QPen(_ACCENT, 46, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    # 以 (745, 330) 为圆心的右开口弧（-50°..+50°）。
    p.setPen(wave_pen_inner)
    p.drawArc(QRectF(745 - 85, 330 - 85, 170, 170), -50 * 16, 100 * 16)
    p.setPen(wave_pen_outer)
    p.drawArc(QRectF(745 - 160, 330 - 160, 320, 320), -50 * 16, 100 * 16)

    p.end()
    return img


def _png_bytes(img: QImage) -> bytes:
    """把 QImage 编码为 PNG 字节串。"""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def _write_ico(path: Path, entries: list[tuple[int, bytes]]) -> None:
    """把多张 PNG 字节图按 ICO 容器格式写出（PNG 压缩条目，Vista+ 支持）。

    ICO 结构：ICONDIR(6B) + N * ICONDIRENTRY(16B) + N 段图像数据。
    宽高字段为 1 字节，256 记为 0。
    """
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dir_entries: list[bytes] = []
    body = bytearray()
    for size, data in entries:
        dim = size if size < 256 else 0
        dir_entries.append(
            struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        )
        offset += len(data)
        body += data
    path.write_bytes(header + b"".join(dir_entries) + bytes(body))


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "assets" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    del app  # 仅需 QGuiApplication 存在以支持字体/图像管线，无需事件循环。

    master = _render_master()

    # 多尺寸 ICO。
    entries: list[tuple[int, bytes]] = []
    for size in _ICON_SIZES:
        scaled = master.scaled(
            size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
        )
        entries.append((size, _png_bytes(scaled)))
    ico_path = out_dir / "dictashift.ico"
    _write_ico(ico_path, entries)

    # 界面 logo 用 PNG（512）。
    png_img = master.scaled(
        _PNG_SIZE, _PNG_SIZE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    )
    png_path = out_dir / "dictashift.png"
    png_img.save(str(png_path), "PNG")

    print(f"[gen_icon] ICO: {ico_path} (sizes={list(_ICON_SIZES)})")
    print(f"[gen_icon] PNG: {png_path} ({_PNG_SIZE}x{_PNG_SIZE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
