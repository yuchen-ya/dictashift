"""DictaShift 打包脚本：调用 PyInstaller（spec 模式）输出 dist/DictaShift/。

用法（在仓库根目录执行）：
    uv run python scripts/build.py

产物：dist/DictaShift/DictaShift.exe（one-dir、windowed）。
打包不含模型文件，模型走 GitHub Releases 应用内下载。

本脚本不动用 git，不发布、不上传。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    # scripts/build.py -> parents[0] = scripts, parents[1] = repo root
    return Path(__file__).resolve().parents[1]


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def main() -> int:
    root = _repo_root()
    spec = root / "scripts" / "DictaShift.spec"
    dist = root / "dist"
    build = root / "build"

    if not spec.exists():
        print(f"[build] 找不到 spec：{spec}", file=sys.stderr)
        return 1

    # 清理旧产物，避免 PyInstaller 增量混淆体积统计。
    for stale in (dist, build):
        if stale.exists():
            print(f"[build] 清理旧目录 {stale}")
            shutil.rmtree(stale, ignore_errors=True)

    # 在仓库根目录调用 PyInstaller，spec 内的相对路径（../src）以仓库根为基准。
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(spec),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(build),
    ]
    print(f"[build] 运行：{' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(root))
    if result.returncode != 0:
        print(f"[build] PyInstaller 失败，返回码 {result.returncode}", file=sys.stderr)
        return result.returncode

    product = dist / "DictaShift"
    exe = product / "DictaShift.exe"
    if not exe.exists():
        print(f"[build] 未找到产物 exe：{exe}", file=sys.stderr)
        return 1

    size = _dir_size(product)
    print("[build] 打包完成")
    print(f"[build] 产物目录：{product}")
    print(f"[build] 启动器：  {exe}")
    print(f"[build] 总大小：  {_human_size(size)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
