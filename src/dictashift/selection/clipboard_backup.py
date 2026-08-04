"""剪贴板全格式备份 / 还原 (Win32 API + ctypes，不依赖 Qt)。

用途：划词取词需要模拟 Ctrl+C，这会覆盖用户剪贴板。取词前先把剪贴板
全部格式快照下来，取词后原样放回，用户无感知。

不变量：
- backup_clipboard 返回的 ClipboardSnapshot 拥有其全部句柄副本的所有权，
  直到 restore() 成功 SetClipboardData 把所有权移交系统。
- 任何异常路径不得泄漏句柄：备份中途失败时释放已复制的句柄；
  SetClipboardData 失败时释放对应句柄；剪贴板无论如何都 CloseClipboard。
- 原剪贴板为空是合法状态：entries 为空，restore 等价于 EmptyClipboard。
- 单个格式不可复制（读取失败 / GDI 复制失败）不中断整体备份，跳过并计数。

可测试性：所有 Win32 调用集中在模块顶部薄封装 (_open_clipboard /
_enum_clipboard_formats / _read_global_bytes 等)，测试 monkeypatch
这些函数即可用 dict 模拟系统剪贴板，无需真实 Win32 句柄。
"""

from __future__ import annotations

import ctypes
import logging
import struct
import time
from ctypes import wintypes
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ClipboardBackupError(RuntimeError):
    """剪贴板备份 / 还原失败。"""


# ==================== Win32 常量 ====================

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_DIB = 8
CF_PALETTE = 9
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_DSPTEXT = 0x0081
CF_DSPBITMAP = 0x0082
CF_DSPMETAFILEPICT = 0x0083
CF_DSPENHMETAFILE = 0x008E

_GMEM_MOVEABLE = 0x0002
_IMAGE_BITMAP = 0

# ==================== 函数原型绑定 ====================

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]

# GlobalAlloc / GlobalFree / GlobalLock / GlobalUnlock / GlobalSize
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]

# 剪贴板基础操作
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EnumClipboardFormats.restype = wintypes.UINT
_user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = ctypes.c_void_p  # HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.SetClipboardData.restype = ctypes.c_void_p  # HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

# CopyImage：复制 HBITMAP (IMAGE_BITMAP)。
_user32.CopyImage.restype = ctypes.c_void_p  # HANDLE
_user32.CopyImage.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]

# GDI 对象复制 / 释放
_gdi32.CopyMetaFileW.restype = ctypes.c_void_p  # HMETAFILE
_gdi32.CopyMetaFileW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_gdi32.CopyEnhMetaFileW.restype = ctypes.c_void_p  # HENHMETAFILE
_gdi32.CopyEnhMetaFileW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_gdi32.DeleteMetaFile.restype = wintypes.BOOL
_gdi32.DeleteMetaFile.argtypes = [ctypes.c_void_p]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.GetObjectW.restype = ctypes.c_int
_gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetPaletteEntries.restype = wintypes.UINT
_gdi32.GetPaletteEntries.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
_gdi32.CreatePalette.restype = ctypes.c_void_p  # HPALETTE
_gdi32.CreatePalette.argtypes = [ctypes.c_void_p]


class _METAFILEPICT(ctypes.Structure):
    _fields_ = (
        ("mm", wintypes.LONG),
        ("xExt", wintypes.LONG),
        ("yExt", wintypes.LONG),
        ("hMF", ctypes.c_void_p),  # HMETAFILE
    )


# ==================== Win32 薄封装 (测试 monkeypatch 点) ====================


def _open_clipboard() -> bool:
    """OpenClipboard(None)；成功返回 True (调用方负责 _close_clipboard)。

    对瞬态失败做短重试：其他程序短暂持有剪贴板锁是常见竞争，
    以 ~10ms 间隔重试最多 5 次 (共约 50ms)，应对常见瞬态情况。
    """
    for _ in range(5):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _close_clipboard() -> None:
    _user32.CloseClipboard()


def _empty_clipboard() -> None:
    if not _user32.EmptyClipboard():
        raise ClipboardBackupError(f"EmptyClipboard 失败，GetLastError={ctypes.get_last_error()}")


def _enum_clipboard_formats() -> list[int]:
    """枚举当前剪贴板全部格式 ID (要求剪贴板已打开)。"""
    formats: list[int] = []
    fmt = _user32.EnumClipboardFormats(0)
    while fmt:
        formats.append(fmt)
        fmt = _user32.EnumClipboardFormats(fmt)
    return formats


def _get_clipboard_data(fmt: int) -> int | None:
    """GetClipboardData 返回只读句柄 (所有权仍在系统)；无数据返回 None。"""
    handle = _user32.GetClipboardData(fmt)
    return handle or None


def _set_clipboard_data(fmt: int, handle: int) -> bool:
    """SetClipboardData；成功后句柄所有权移交系统。"""
    return bool(_user32.SetClipboardData(fmt, handle))


def _read_global_bytes(handle: int) -> bytes:
    """GlobalSize + GlobalLock 读取 HGLOBAL 原始字节。失败抛 ClipboardBackupError。"""
    size = _kernel32.GlobalSize(handle)
    if not size:
        raise ClipboardBackupError(f"GlobalSize 失败，GetLastError={ctypes.get_last_error()}")
    locked = _kernel32.GlobalLock(handle)
    if not locked:
        raise ClipboardBackupError(f"GlobalLock 失败，GetLastError={ctypes.get_last_error()}")
    try:
        return ctypes.string_at(locked, size)
    finally:
        _kernel32.GlobalUnlock(handle)


def _alloc_global_bytes(data: bytes) -> int:
    """GlobalAlloc(GMEM_MOVEABLE) + 写入字节，返回 HGLOBAL。失败抛异常且不泄漏。"""
    h_global = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not h_global:
        raise ClipboardBackupError(f"GlobalAlloc 失败，GetLastError={ctypes.get_last_error()}")
    locked = _kernel32.GlobalLock(h_global)
    if not locked:
        _kernel32.GlobalFree(h_global)
        raise ClipboardBackupError(f"GlobalLock 失败，GetLastError={ctypes.get_last_error()}")
    try:
        ctypes.memmove(locked, data, len(data))
    finally:
        _kernel32.GlobalUnlock(h_global)
    return h_global


def _free_global(handle: int) -> None:
    _kernel32.GlobalFree(handle)


def _delete_gdi_object(handle: int) -> None:
    _gdi32.DeleteObject(handle)


def _copy_bitmap(handle: int) -> int | None:
    """CopyImage 复制 HBITMAP (CF_BITMAP / CF_DSPBITMAP)。"""
    copied = _user32.CopyImage(handle, _IMAGE_BITMAP, 0, 0, 0)
    return copied or None


def _copy_palette(handle: int) -> int | None:
    """CopyPalette 等效实现：GetPaletteEntries 读调色板项 + CreatePalette 新建。

    gdi32 不导出 CopyPalette (SDK 里是宏/内联)，这是标准等效路径。
    """
    count = wintypes.WORD(0)
    # 对 HPALETTE，GetObjectW 返回调色板项数 (sizeof(WORD))。
    if not _gdi32.GetObjectW(handle, ctypes.sizeof(count), ctypes.byref(count)):
        return None
    n = count.value
    if n <= 0:
        return None
    # LOGPALETTE = palVersion(WORD) + palNumEntries(WORD) + PALETTEENTRY[n](4n 字节)
    buf = (ctypes.c_byte * (4 + 4 * n))()
    struct.pack_into("<HH", buf, 0, 0x300, n)
    got = _gdi32.GetPaletteEntries(handle, 0, n, ctypes.byref(buf, 4))
    if not got:
        return None
    copied = _gdi32.CreatePalette(ctypes.byref(buf))
    return copied or None


def _copy_metafilepict(handle: int) -> int | None:
    """复制 CF_METAFILEPICT：CopyMetaFileW 复制内层 HMETAFILE，重建 METAFILEPICT。

    返回值是 HGLOBAL (内存句柄，用 GlobalFree 释放；失败路径另需 DeleteMetaFile
    内层句柄，见 _free_saved_entry)。
    """
    locked = _kernel32.GlobalLock(handle)
    if not locked:
        return None
    try:
        pict = _METAFILEPICT.from_address(locked)
        mm, x_ext, y_ext, h_mf = pict.mm, pict.xExt, pict.yExt, pict.hMF
    finally:
        _kernel32.GlobalUnlock(handle)
    if not h_mf:
        return None
    new_mf = _gdi32.CopyMetaFileW(h_mf, None)
    if not new_mf:
        return None
    # 用 ctypes 结构序列化，保证 64 位下 hMF 的 8 字节对齐与系统解析一致。
    new_pict = _METAFILEPICT(mm, x_ext, y_ext, new_mf)
    data = ctypes.string_at(ctypes.byref(new_pict), ctypes.sizeof(new_pict))
    try:
        return _alloc_global_bytes(data)
    except ClipboardBackupError:
        _gdi32.DeleteMetaFile(new_mf)
        return None


def _copy_enhmetafile(handle: int) -> int | None:
    """CopyEnhMetaFileW 复制 HENHMETAFILE (CF_ENHMETAFILE)。"""
    copied = _gdi32.CopyEnhMetaFileW(handle, None)
    return copied or None


# 副本是 HGLOBAL 容器、内含指针指向需 DeleteMetaFile 的 HMETAFILE 的格式。
# CF_METAFILEPICT 与 CF_DSPMETAFILEPICT 句柄布局相同，必须走 _copy_metafilepict，
# 否则原始字节路径会把含内层 hMF 指针的结构体直接复制，还原后是悬空指针。
_METAFILEPICT_FORMATS = frozenset({CF_METAFILEPICT, CF_DSPMETAFILEPICT})

# 需要 GDI 专用复制的格式 -> 复制函数；其余格式按原始字节复制。
_GDI_COPY_FUNCS = {
    CF_BITMAP: _copy_bitmap,
    CF_DSPBITMAP: _copy_bitmap,
    CF_PALETTE: _copy_palette,
    CF_METAFILEPICT: _copy_metafilepict,
    CF_DSPMETAFILEPICT: _copy_metafilepict,
    CF_ENHMETAFILE: _copy_enhmetafile,
    CF_DSPENHMETAFILE: _copy_enhmetafile,
}


# ==================== 备份 / 还原编排 ====================


@dataclass
class _SavedFormat:
    """单个已复制的格式。handle 所有权归 ClipboardSnapshot，直到 SetClipboardData。"""

    fmt: int
    handle: int
    is_gdi: bool  # True=GDI 对象 (DeleteObject)；False=HGLOBAL (GlobalFree)


@dataclass
class ClipboardSnapshot:
    """剪贴板全格式快照。restore() 把所有格式放回剪贴板。"""

    _entries: list[_SavedFormat] = field(default_factory=list)
    skipped: int = 0  # 备份时不可复制而跳过的格式数
    restore_failures: int = 0  # restore 时 SetClipboardData 失败的格式数

    @property
    def is_empty(self) -> bool:
        """原剪贴板为空 (无任何可保存格式)。"""
        return not self._entries

    @property
    def formats(self) -> list[int]:
        """已保存的格式 ID 列表 (诊断 / 测试用)。"""
        return [e.fmt for e in self._entries]

    def restore(self) -> None:
        """清空剪贴板并逐格式放回。SetClipboardData 失败的格式释放句柄并计数。

        OpenClipboard / EmptyClipboard 失败抛 ClipboardBackupError；
        单格式 SetClipboardData 失败不中断其余格式。
        无论成功与否，调用后本快照不再持有任何句柄 (所有权已移交或已释放)。
        """
        entries = self._entries
        self._entries = []
        if not _open_clipboard():
            # 打开失败：句柄所有权仍在我们手上，必须释放避免泄漏。
            for entry in entries:
                _free_saved_entry(entry)
            raise ClipboardBackupError(
                f"OpenClipboard 失败，GetLastError={ctypes.get_last_error()}"
            )
        try:
            _empty_clipboard()
            failures = 0
            for entry in entries:
                if _set_clipboard_data(entry.fmt, entry.handle):
                    continue  # 所有权移交系统
                failures += 1
                logger.warning(
                    "[ClipboardSnapshot] SetClipboardData 失败 fmt=%s，GetLastError=%s",
                    entry.fmt,
                    ctypes.get_last_error(),
                )
                _free_saved_entry(entry)
            self.restore_failures = failures
        finally:
            _close_clipboard()


def _copy_format_handle(fmt: int, handle: int) -> tuple[int, bool] | None:
    """复制单个格式句柄。返回 (新句柄, is_gdi)；不可复制返回 None。"""
    copy_fn = _GDI_COPY_FUNCS.get(fmt)
    if copy_fn is not None:
        copied = copy_fn(handle)
        if not copied:
            return None
        # METAFILEPICT 类格式的副本是 HGLOBAL (内存句柄)，其余是 GDI 对象。
        return copied, fmt not in _METAFILEPICT_FORMATS
    data = _read_global_bytes(handle)
    return _alloc_global_bytes(data), False


def _free_saved_entry(entry: _SavedFormat) -> None:
    """释放一个未移交系统的句柄副本；异常只记日志 (清理路径不再抛错)。"""
    try:
        if entry.fmt in _METAFILEPICT_FORMATS:
            # 副本是 METAFILEPICT HGLOBAL，内层 HMETAFILE 需单独 DeleteMetaFile。
            locked = _kernel32.GlobalLock(entry.handle)
            if locked:
                try:
                    h_mf = _METAFILEPICT.from_address(locked).hMF
                finally:
                    _kernel32.GlobalUnlock(entry.handle)
                if h_mf:
                    _gdi32.DeleteMetaFile(h_mf)
            _free_global(entry.handle)
        elif entry.is_gdi:
            _delete_gdi_object(entry.handle)
        else:
            _free_global(entry.handle)
    except Exception:  # noqa: BLE001 - 清理路径吞异常，避免遮蔽主错误
        logger.warning("[ClipboardSnapshot] 释放句柄失败 fmt=%s", entry.fmt, exc_info=True)


def backup_clipboard() -> ClipboardSnapshot:
    """备份当前剪贴板全部格式，返回 ClipboardSnapshot。

    流程：OpenClipboard -> EnumClipboardFormats -> 逐格式 GetClipboardData +
    复制句柄 (读取动作本身会强制渲染延迟格式) -> CloseClipboard。
    单个格式失败跳过并计数；OpenClipboard 失败抛 ClipboardBackupError；
    备份中途异常时释放已复制的全部句柄后重新抛出。
    """
    if not _open_clipboard():
        raise ClipboardBackupError(
            f"OpenClipboard 失败，GetLastError={ctypes.get_last_error()}"
        )
    entries: list[_SavedFormat] = []
    skipped = 0
    try:
        try:
            for fmt in _enum_clipboard_formats():
                handle = _get_clipboard_data(fmt)
                if not handle:
                    skipped += 1
                    continue
                try:
                    copied = _copy_format_handle(fmt, handle)
                except Exception:  # noqa: BLE001 - 单格式失败不中断整体备份
                    logger.warning(
                        "[backup_clipboard] 格式 %s 复制失败，跳过", fmt, exc_info=True
                    )
                    skipped += 1
                    continue
                if copied is None:
                    skipped += 1
                    continue
                entries.append(_SavedFormat(fmt=fmt, handle=copied[0], is_gdi=copied[1]))
        except Exception:
            # 备份中途失败：释放已复制句柄，避免泄漏。
            for entry in entries:
                _free_saved_entry(entry)
            raise
    finally:
        _close_clipboard()
    return ClipboardSnapshot(_entries=entries, skipped=skipped)
