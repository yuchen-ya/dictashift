"""下载并校验 SenseVoice 模型，再安装到用户模型目录。"""

from __future__ import annotations

import hashlib
import os
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

# 下载后完整性校验（SHA256）。防止 Release 资产被替换为同大小的篡改文件
# （开源安全审查 H2）。发布新模型包时必须同步更新。
EXPECTED_HASHES = {
    "SenseVoice-Encoder.int8.onnx": "efecf25c231268c11f23a0431cec92f4570003efdefc9c97530891298ebe2772",
    "SenseVoice-CTC.int8.onnx": "8a2217e07324a394575af470f2045add2eb2d1dc898ff8610fdef9936f4c18d9",
    "Tokenizer.bpe.model": "aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8",
}

# 下载阶段累积字节上限：期望总大小的 1.5 倍（约 363MB），
# 防止异常响应耗尽磁盘（开源安全审查 M1）。
_MAX_DOWNLOAD_BYTES = int(sum(EXPECTED_SIZES.values()) * 1.5)

# SHA256 分块读取大小（1MB）：不把 220MB+ 的 encoder 整读进内存。
# 桌面 UA，避免某些 CDN 的默认拦截。
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "DictaShift/0.2.0"
)

ProgressCb = Callable[[int, int], None]


class ModelDownloadError(RuntimeError):
    """模型下载/解压/校验/安装失败的可读错误。"""


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


class _ModelRestoreError(ModelDownloadError):
    """安装失败，且有旧文件需从备份目录恢复。"""


def _install_models(staging_dir: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    backups = staging_dir / "previous"
    backups.mkdir()
    saved = []
    installed = []
    try:
        for name in REQUIRED_MODEL_FILES:
            target = dest_dir / name
            if target.exists() or target.is_symlink():
                if not target.is_file() and not target.is_symlink():
                    raise OSError(f"模型目标不是文件：{name}")
                os.replace(target, backups / name)
                saved.append(name)
            os.replace(staging_dir / name, target)
            installed.append(name)
    except OSError as exc:
        restore_failed = False
        for name in reversed(saved):
            try:
                os.replace(backups / name, dest_dir / name)
            except OSError:
                restore_failed = True
        for name in installed:
            if name not in saved:
                try:
                    (dest_dir / name).unlink(missing_ok=True)
                except OSError:
                    restore_failed = True
        if restore_failed:
            raise _ModelRestoreError(f"模型安装失败，旧文件备份保留在 {backups}") from exc
        raise ModelDownloadError("模型安装失败，请检查文件是否被占用并重试") from exc


def download_models(
    dest_dir: Path,
    progress_cb: ProgressCb | None = None,
    expected_hashes: dict[str, str] | None = None,
) -> None:
    """下载、校验并安装模型，失败时保留已安装的模型。

    progress_cb(downloaded_bytes, total_bytes) 的总大小在未知时为 -1。
    """
    if expected_hashes is None:
        expected_hashes = EXPECTED_HASHES
    dest_dir = Path(dest_dir)
    staging_dir: Path | None = None

    def _emit(downloaded: int, total: int) -> None:
        if progress_cb is not None:
            try:
                progress_cb(downloaded, total)
            except Exception:  # noqa: BLE001
                # 进度回调失败不应中断下载。
                pass

    try:
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=f".{dest_dir.name}.tmp-", dir=dest_dir.parent))
        tmp_zip = staging_dir / "download.zip"
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
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise ModelDownloadError(
                            f"下载中止：声明大小 {total} 字节超出安全上限"
                        )
                _emit(0, total)
                with tmp_zip.open("xb") as f:
                    for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD_BYTES:
                            raise ModelDownloadError(
                                "下载中止：累积大小超出安全上限（文件异常）"
                            )
                        f.write(chunk)
                        _emit(downloaded, total)

        # 2. 解压目标文件到临时目录（仅 EXPECTED_SIZES 里的三个文件名）。
        wanted = set(REQUIRED_MODEL_FILES)
        with zipfile.ZipFile(tmp_zip) as zf:
            # 建立期望文件名 -> zip 内条目名 的映射（取 basename 匹配）。
            member_for: dict[str, zipfile.ZipInfo] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = Path(info.filename).name
                if base in wanted:
                    member_for[base] = info
            missing = wanted - set(member_for.keys())
            if missing:
                raise ModelDownloadError(
                    f"压缩包缺少模型文件：{sorted(missing)}"
                )
            # 提取，逐条做安全检查。
            for base, member in member_for.items():
                if _is_unsafe_name(member.filename):
                    raise ModelDownloadError(
                        f"压缩包含可疑路径条目，已拒绝：{member.filename}"
                    )
                expected = EXPECTED_SIZES[base]
                if member.file_size != expected:
                    raise ModelDownloadError(
                        f"文件大小校验失败：{base} 实际 {member.file_size}，期望 {expected}"
                    )
                target = staging_dir / base
                with zf.open(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)

        # 3. 校验（仍在临时目录）：三个文件存在、大小匹配、SHA256 匹配。
        for name in REQUIRED_MODEL_FILES:
            target = staging_dir / name
            if not target.is_file():
                raise ModelDownloadError(f"解压后缺失文件：{name}")
            actual = target.stat().st_size
            expected = EXPECTED_SIZES[name]
            if actual != expected:
                raise ModelDownloadError(
                    f"文件大小校验失败：{name} 实际 {actual}，期望 {expected}"
                )
            expected_hash = expected_hashes.get(name)
            if expected_hash:
                with target.open("rb") as file:
                    digest = hashlib.file_digest(file, "sha256").hexdigest()
                if digest != expected_hash:
                    raise ModelDownloadError(
                        f"文件完整性校验失败（SHA256 不匹配）：{name}"
                    )

        _install_models(staging_dir, dest_dir)

    except _ModelRestoreError:
        staging_dir = None  # 保留需要手动恢复的旧文件。
        raise
    except httpx.HTTPError as e:
        raise ModelDownloadError(f"网络下载失败：{type(e).__name__}") from e
    except zipfile.BadZipFile as e:
        raise ModelDownloadError("下载的文件不是有效的 zip 压缩包") from e
    except OSError as e:
        raise ModelDownloadError("写入模型失败，请检查磁盘空间和目录权限") from e
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
