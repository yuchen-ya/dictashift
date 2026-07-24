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

import hashlib
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
    expected_hashes: dict[str, str] | None = None,
) -> None:
    """从 MODEL_DOWNLOAD_URL 流式下载 zip 到临时文件 -> 解压到 dest_dir ->
    校验三个文件存在且大小、SHA256 匹配 -> 清理临时文件。

    progress_cb(downloaded_bytes, total_bytes)；total 未知（响应无
    Content-Length）时传 -1。
    expected_hashes：默认使用模块常量 EXPECTED_HASHES（测试可注入）。

    任何失败抛 ModelDownloadError，并清理半成品：
    - 已解压但校验失败/拒绝条目的文件会被删除。
    - 临时下载文件始终清理。
    """
    if expected_hashes is None:
        expected_hashes = EXPECTED_HASHES
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
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise ModelDownloadError(
                            f"下载中止：声明大小 {total} 字节超出安全上限"
                        )
                _emit(0, total)
                with open(tmp_zip, "wb") as f:
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

        # 3. 校验：三个文件存在且大小、SHA256 匹配。
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
            expected_hash = expected_hashes.get(name)
            if expected_hash:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if digest != expected_hash:
                    raise ModelDownloadError(
                        f"文件完整性校验失败（SHA256 不匹配）：{name}"
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
