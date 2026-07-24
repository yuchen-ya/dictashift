"""模型下载器：从 GitHub Releases 流式下载 zip 并解压到用户模型目录。

不变量 (AGENTS.md 11 / 12)：
- 只用 httpx（已是项目依赖），无新第三方依赖。
- httpx stream 下载：follow_redirects=True（GitHub releases 会 302），
  connect 超时 15s / read 超时 60s，桌面 UA。
- 解压：只提取 EXPECTED_SIZES 里的三个文件名，忽略 zip 内其它条目；
  拒绝含 `..` 或绝对路径的条目（防路径穿越）。
- 下载后校验：三个文件都存在且大小匹配 EXPECTED_SIZES；任一不满足 -> 抛
  ModelDownloadError，并清理已解压的半成品。
- progress_cb(downloaded, total)：total 未知时传 -1。
- 错误消息可读、不含响应头、不含敏感信息。
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from .providers import REQUIRED_MODEL_FILES

# GitHub Releases 资产地址（v0.2.0 首版模型包）。
MODEL_DOWNLOAD_URL = (
    "https://github.com/yuchen-ya/dictashift/releases/download/"
    "v0.2.0/sensevoice_onnx-int8.zip"
)

# 下载后校验用的期望大小（字节）。键与 REQUIRED_MODEL_FILES 一致。
EXPECTED_SIZES = {
    "SenseVoice-Encoder.int8.onnx": 228607767,
    "SenseVoice-CTC.int8.onnx": 13073048,
    "Tokenizer.bpe.model": 377341,
}

# 桌面 UA，避免某些 CDN 的默认拦截。
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "DictaShift/0.2.0"
)

ProgressCb = Callable[[int, int], None]


class ModelDownloadError(RuntimeError):
    """模型下载/解压/校验失败的可读错误。"""


def _is_unsafe_name(name: str) -> bool:
    """判断 zip 条目名是否危险：绝对路径或含 `..`。"""
    # 绝对路径（Windows 盘符或 POSIX 根）。
    if name.startswith("/") or name.startswith("\\"):
        return True
    if len(name) >= 2 and name[1] == ":":
        return True
    parts = name.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        return True
    return False


def download_models(
    dest_dir: Path,
    progress_cb: ProgressCb | None = None,
) -> None:
    """从 MODEL_DOWNLOAD_URL 流式下载 zip 到临时文件 -> 解压到 dest_dir ->
    校验三个文件存在且大小匹配 -> 清理临时文件。

    progress_cb(downloaded_bytes, total_bytes)；total 未知（响应无
    Content-Length）时传 -1。

    任何失败抛 ModelDownloadError，并清理半成品：
    - 已解压但校验失败/拒绝条目的文件会被删除。
    - 临时下载文件始终清理。
    """
    dest_dir = Path(dest_dir)
    tmp_zip: Path | None = None
    extracted: list[Path] = []

    def _emit(downloaded: int, total: int) -> None:
        if progress_cb is not None:
            try:
                progress_cb(downloaded, total)
            except Exception:  # noqa: BLE001
                # 进度回调失败不应中断下载。
                pass

    try:
        # 1. 流式下载到临时文件。
        tmp_zip = Path(
            tempfile.mktemp(prefix="dictashift-model-", suffix=".zip")
        )
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)
        total = -1
        downloaded = 0
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _DESKTOP_UA},
        ) as client:
            with client.stream("GET", MODEL_DOWNLOAD_URL) as resp:
                if resp.status_code != 200:
                    raise ModelDownloadError(
                        f"下载失败：HTTP {resp.status_code}"
                    )
                cl = resp.headers.get("Content-Length")
                if cl is not None and cl.isdigit():
                    total = int(cl)
                _emit(0, total)
                with open(tmp_zip, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        _emit(downloaded, total)

        # 2. 解压目标文件（仅 EXPECTED_SIZES 里的三个文件名）。
        dest_dir.mkdir(parents=True, exist_ok=True)
        wanted = set(REQUIRED_MODEL_FILES)
        with zipfile.ZipFile(tmp_zip) as zf:
            # 建立期望文件名 -> zip 内条目名 的映射（取 basename 匹配）。
            member_for: dict[str, str] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = Path(info.filename).name
                if base in wanted:
                    member_for[base] = info.filename
            missing = wanted - set(member_for.keys())
            if missing:
                raise ModelDownloadError(
                    f"压缩包缺少模型文件：{sorted(missing)}"
                )
            # 提取，逐条做安全检查。
            for base, member in member_for.items():
                if _is_unsafe_name(member):
                    raise ModelDownloadError(
                        f"压缩包含可疑路径条目，已拒绝：{member}"
                    )
                target = dest_dir / base
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(target)

        # 3. 校验：三个文件存在且大小匹配。
        for name in REQUIRED_MODEL_FILES:
            target = dest_dir / name
            if not target.exists():
                raise ModelDownloadError(f"解压后缺失文件：{name}")
            actual = target.stat().st_size
            expected = EXPECTED_SIZES[name]
            if actual != expected:
                raise ModelDownloadError(
                    f"文件大小校验失败：{name} 实际 {actual}，期望 {expected}"
                )

    except ModelDownloadError:
        # 清理半成品。
        for p in extracted:
            try:
                p.unlink()
            except OSError:
                pass
        raise
    except httpx.HTTPError as e:
        # 清理半成品后包装为可读错误。
        for p in extracted:
            try:
                p.unlink()
            except OSError:
                pass
        raise ModelDownloadError(f"网络下载失败：{type(e).__name__}") from e
    except zipfile.BadZipFile as e:
        for p in extracted:
            try:
                p.unlink()
            except OSError:
                pass
        raise ModelDownloadError("下载的文件不是有效的 zip 压缩包") from e
    finally:
        if tmp_zip is not None:
            try:
                tmp_zip.unlink()
            except OSError:
                pass
