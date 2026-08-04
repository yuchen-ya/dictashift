"""划词取词测试：clipboard_backup / capture / SelectionService。

测试目标：
- clipboard_backup：fake 剪贴板 (dict 模拟 fmt->bytes) 验证备份/还原编排、
  空剪贴板、不可复制格式跳过计数、SetClipboardData 失败时句柄释放、GDI 格式路径。
- capture：本进程守卫 skipped_self；序列号未变 no_text 且不还原；无文本/空白
  no_text 且还原；超长 too_long；正常 success；simulate_copy 抛错也完成还原。
- SelectionService：拖拽阈值（含从右往左划选各向同性）/ 冷却期抑制；
  on_press_activity 任意左键按下通知；start/stop 生命周期。

Win32 / pynput 全部 monkeypatch 掉，不依赖真实剪贴板与全局监听。
"""

from __future__ import annotations

import os
import queue

import pytest
from pynput import mouse

import dictashift.selection.capture as capture
import dictashift.selection.clipboard_backup as cb
from dictashift.selection.capture import CaptureStatus
from dictashift.selection.service import SelectionService


# ==================== fake 剪贴板 (dict 模拟 fmt -> bytes) ====================


class _FakeClipboard:
    """用 dict 模拟系统剪贴板；句柄是递增整数 token，handles 记录 token->bytes。"""

    def __init__(self, data: dict[int, bytes]):
        self.data = dict(data)
        self.handles: dict[int, bytes] = {}
        self._next = 100
        self.freed: list[int] = []  # 被 GlobalFree/DeleteObject 的 token
        self.set_calls: list[int] = []  # SetClipboardData 的 fmt 序列
        self.empty_calls = 0
        self.fail_set_for: set[int] = set()  # 模拟 SetClipboardData 失败的 fmt

    # ---- 薄封装替身 (与 cb 模块顶部函数一一对应) ----

    def open(self) -> bool:
        return True

    def close(self) -> None:
        return None

    def empty(self) -> None:
        self.empty_calls += 1
        self.data.clear()

    def enum(self) -> list[int]:
        return list(self.data.keys())

    def get_data(self, fmt: int) -> int | None:
        token = self._next
        self._next += 1
        self.handles[token] = self.data[fmt]
        return token

    def set_data(self, fmt: int, handle: int) -> bool:
        if fmt in self.fail_set_for:
            return False
        self.set_calls.append(fmt)
        self.data[fmt] = self.handles.pop(handle)
        return True

    def read_global(self, handle: int) -> bytes:
        return self.handles[handle]

    def alloc_global(self, data: bytes) -> int:
        token = self._next
        self._next += 1
        self.handles[token] = bytes(data)
        return token

    def free_handle(self, handle: int) -> None:
        self.freed.append(handle)
        self.handles.pop(handle, None)


def _patch_clipboard(monkeypatch: pytest.MonkeyPatch, fake: _FakeClipboard) -> None:
    """把 cb 模块顶部薄封装全部替换为 fake 剪贴板实现。"""
    monkeypatch.setattr(cb, "_open_clipboard", fake.open)
    monkeypatch.setattr(cb, "_close_clipboard", fake.close)
    monkeypatch.setattr(cb, "_empty_clipboard", fake.empty)
    monkeypatch.setattr(cb, "_enum_clipboard_formats", fake.enum)
    monkeypatch.setattr(cb, "_get_clipboard_data", fake.get_data)
    monkeypatch.setattr(cb, "_set_clipboard_data", fake.set_data)
    monkeypatch.setattr(cb, "_read_global_bytes", fake.read_global)
    monkeypatch.setattr(cb, "_alloc_global_bytes", fake.alloc_global)
    monkeypatch.setattr(cb, "_free_global", fake.free_handle)
    monkeypatch.setattr(cb, "_delete_gdi_object", fake.free_handle)


_PRIVATE_FMT = 49381  # 未注册的私有格式 ID


# ==================== clipboard_backup ====================


def test_backup_restore_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """备份 -> 剪贴板被取词覆盖 -> 还原后格式集合与内容一致。"""
    original = {
        cb.CF_UNICODETEXT: "你好 world".encode("utf-16-le") + b"\x00\x00",
        _PRIVATE_FMT: b"\x01\x02\x03",
    }
    fake = _FakeClipboard(original)
    _patch_clipboard(monkeypatch, fake)

    snapshot = cb.backup_clipboard()
    assert not snapshot.is_empty
    assert snapshot.skipped == 0
    assert sorted(snapshot.formats) == sorted(original.keys())

    # 模拟取词过程覆盖剪贴板
    fake.data.clear()
    fake.data[cb.CF_UNICODETEXT] = "取词结果".encode("utf-16-le") + b"\x00\x00"

    snapshot.restore()
    assert fake.empty_calls == 1
    assert fake.data == original
    assert sorted(fake.set_calls) == sorted(original.keys())
    assert snapshot.restore_failures == 0


def test_backup_empty_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """原剪贴板为空：可正确表示 (is_empty)，还原即 EmptyClipboard。"""
    fake = _FakeClipboard({})
    _patch_clipboard(monkeypatch, fake)

    snapshot = cb.backup_clipboard()
    assert snapshot.is_empty
    assert snapshot.skipped == 0

    fake.data[cb.CF_UNICODETEXT] = b"x\x00"
    snapshot.restore()
    assert fake.empty_calls == 1
    assert fake.data == {}
    assert fake.set_calls == []


def test_backup_uncopyable_format_skipped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetClipboardData 无数据的格式跳过并计数，不中断整体备份。"""
    fake = _FakeClipboard(
        {
            cb.CF_UNICODETEXT: "ok".encode("utf-16-le") + b"\x00\x00",
            _PRIVATE_FMT: b"\xff",
        }
    )
    _patch_clipboard(monkeypatch, fake)

    original_get_data = fake.get_data

    def get_data_no_private(fmt: int):
        if fmt == _PRIVATE_FMT:
            return None
        return original_get_data(fmt)

    monkeypatch.setattr(cb, "_get_clipboard_data", get_data_no_private)

    snapshot = cb.backup_clipboard()
    assert snapshot.skipped == 1
    assert snapshot.formats == [cb.CF_UNICODETEXT]


def test_backup_read_failure_skipped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取某格式抛异常时跳过并计数，其余格式照常备份。"""
    fake = _FakeClipboard(
        {
            cb.CF_UNICODETEXT: "ok".encode("utf-16-le") + b"\x00\x00",
            _PRIVATE_FMT: b"\xff",
        }
    )
    _patch_clipboard(monkeypatch, fake)

    original_read = fake.read_global

    def read_fail_private(handle: int) -> bytes:
        if fake.handles[handle] == b"\xff":
            raise cb.ClipboardBackupError("模拟读取失败")
        return original_read(handle)

    monkeypatch.setattr(cb, "_read_global_bytes", read_fail_private)

    snapshot = cb.backup_clipboard()
    assert snapshot.skipped == 1
    assert snapshot.formats == [cb.CF_UNICODETEXT]


def test_restore_set_failure_frees_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """SetClipboardData 失败的格式：句柄被释放 (不泄漏)、计数、其余格式照常放回。"""
    original = {
        cb.CF_UNICODETEXT: "abc".encode("utf-16-le") + b"\x00\x00",
        _PRIVATE_FMT: b"\x01",
    }
    fake = _FakeClipboard(original)
    _patch_clipboard(monkeypatch, fake)
    fake.fail_set_for.add(_PRIVATE_FMT)

    snapshot = cb.backup_clipboard()
    snapshot.restore()

    assert snapshot.restore_failures == 1
    assert fake.set_calls == [cb.CF_UNICODETEXT]
    assert fake.data[cb.CF_UNICODETEXT] == original[cb.CF_UNICODETEXT]
    assert _PRIVATE_FMT not in fake.data
    # 失败格式的句柄走了释放路径
    assert len(fake.freed) == 1


def test_backup_gdi_format_uses_copy_func(monkeypatch: pytest.MonkeyPatch) -> None:
    """GDI 格式 (CF_BITMAP) 走专用复制函数而非原始字节路径。"""
    fake = _FakeClipboard({cb.CF_BITMAP: b"pretend-bitmap"})
    _patch_clipboard(monkeypatch, fake)

    copy_calls: list[int] = []

    def fake_copy_bitmap(handle: int) -> int:
        copy_calls.append(handle)
        return fake.alloc_global(b"copy-of-" + fake.read_global(handle))

    monkeypatch.setitem(cb._GDI_COPY_FUNCS, cb.CF_BITMAP, fake_copy_bitmap)

    snapshot = cb.backup_clipboard()
    assert len(copy_calls) == 1
    assert snapshot.skipped == 0

    snapshot.restore()
    assert fake.data[cb.CF_BITMAP] == b"copy-of-pretend-bitmap"


def test_backup_gdi_uncopyable_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """GDI 复制函数返回 None -> 跳过并计数，不抛异常。"""
    fake = _FakeClipboard({cb.CF_BITMAP: b"pretend-bitmap"})
    _patch_clipboard(monkeypatch, fake)
    monkeypatch.setitem(cb._GDI_COPY_FUNCS, cb.CF_BITMAP, lambda h: None)

    snapshot = cb.backup_clipboard()
    assert snapshot.is_empty
    assert snapshot.skipped == 1


def test_copy_metafilepict_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """CF_METAFILEPICT 复制：内层 HMETAFILE 经 CopyMetaFileW 复制，
    重建的 METAFILEPICT 字节布局与 ctypes 结构一致 (64 位指针对齐)。"""
    import ctypes

    # 源 METAFILEPICT：真实 ctypes 结构提供正确对齐的字节布局。
    src = cb._METAFILEPICT(1, 200, 300, 0xBEEF)
    src_buf = ctypes.create_string_buffer(
        ctypes.string_at(ctypes.byref(src), ctypes.sizeof(src)), ctypes.sizeof(src)
    )

    class _FakeKernel32:
        @staticmethod
        def GlobalLock(handle: int) -> int:
            return ctypes.addressof(src_buf)

        @staticmethod
        def GlobalUnlock(handle: int) -> None:
            return None

    class _FakeGdi32:
        copied_from: list[int] = []

        @classmethod
        def CopyMetaFileW(cls, h_mf, _unused):
            cls.copied_from.append(h_mf)
            return 0xDEAD

    captured: list[bytes] = []
    monkeypatch.setattr(cb, "_kernel32", _FakeKernel32)
    monkeypatch.setattr(cb, "_gdi32", _FakeGdi32)
    monkeypatch.setattr(cb, "_alloc_global_bytes", lambda data: captured.append(data) or 777)

    handle = cb._copy_metafilepict(12345)
    assert handle == 777
    assert _FakeGdi32.copied_from == [0xBEEF]
    # 还原出的结构字段与源一致，且内层句柄已替换为复制句柄。
    rebuilt = cb._METAFILEPICT.from_buffer_copy(captured[0])
    assert (rebuilt.mm, rebuilt.xExt, rebuilt.yExt) == (1, 200, 300)
    assert rebuilt.hMF == 0xDEAD


def test_dsp_metafile_formats_route_to_gdi_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """CF_DSPMETAFILEPICT / CF_DSPENHMETAFILE 走专用 GDI 复制函数 (非原始字节路径)，
    且 DSPMETAFILEPICT 副本被标记为 HGLOBAL (is_gdi=False)。"""
    fake = _FakeClipboard(
        {
            cb.CF_DSPMETAFILEPICT: b"dsp-mf",
            cb.CF_DSPENHMETAFILE: b"dsp-emf",
        }
    )
    _patch_clipboard(monkeypatch, fake)

    mf_calls: list[int] = []
    emf_calls: list[int] = []
    delete_meta_calls: list[int] = []

    def fake_copy_mf(handle: int) -> int:
        mf_calls.append(handle)
        return fake.alloc_global(b"mf-copy-" + fake.read_global(handle))

    def fake_copy_emf(handle: int) -> int:
        emf_calls.append(handle)
        return fake.alloc_global(b"emf-copy-" + fake.read_global(handle))

    monkeypatch.setattr(cb, "_copy_metafilepict", fake_copy_mf)
    monkeypatch.setattr(cb, "_copy_enhmetafile", fake_copy_emf)
    # 重置 _GDI_COPY_FUNCS 以使用被替换后的复制函数 (它是在模块加载时绑定的)。
    monkeypatch.setitem(cb._GDI_COPY_FUNCS, cb.CF_DSPMETAFILEPICT, fake_copy_mf)
    monkeypatch.setitem(cb._GDI_COPY_FUNCS, cb.CF_DSPENHMETAFILE, fake_copy_emf)

    snapshot = cb.backup_clipboard()
    assert snapshot.skipped == 0
    assert len(mf_calls) == 1
    assert len(emf_calls) == 1

    # DSPMETAFILEPICT 副本应标记为 HGLOBAL (is_gdi=False)；DSPENHMETAFILE 标记为 GDI。
    entries_by_fmt = {e.fmt: e for e in snapshot._entries}
    assert entries_by_fmt[cb.CF_DSPMETAFILEPICT].is_gdi is False
    assert entries_by_fmt[cb.CF_DSPENHMETAFILE].is_gdi is True

    # 还原成功，句柄移交系统
    snapshot.restore()
    assert snapshot.restore_failures == 0
    assert fake.data[cb.CF_DSPMETAFILEPICT] == b"mf-copy-dsp-mf"
    assert fake.data[cb.CF_DSPENHMETAFILE] == b"emf-copy-dsp-emf"


def test_dsp_metafilepict_set_failure_runs_free_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """DSPMETAFILEPICT 的 SetClipboardData 失败时走 _free_saved_entry，
    验证 _METAFILEPICT_FORMATS 判定覆盖 DSP 变体 (不抛、句柄被释放、内层 HMETAFILE 被 DeleteMetaFile)。"""
    fake = _FakeClipboard({cb.CF_DSPMETAFILEPICT: b"dsp-mf"})
    _patch_clipboard(monkeypatch, fake)
    # 让该格式走 _copy_metafilepict 路径，但用 fake 实现：返回一个 fake token，
    # 并把内层 HMETAFILE 通过 handles 表登记为已知字节布局 (与 _METAFILEPICT 一致)。
    inner_hmf = 0x1234

    def fake_copy_mf(handle: int):
        token = fake.alloc_global(b"mf-copy")
        # _free_saved_entry 会用 _kernel32.GlobalLock 读 _METAFILEPICT；
        # 这里改写 handles[token] 为 ctypes 序列化的 _METAFILEPICT 字节，
        # 并 patch _kernel32.GlobalLock/Unlock 让其把 token 映射回这些字节。
        import ctypes

        pict = cb._METAFILEPICT(2, 10, 20, inner_hmf)
        fake.handles[token] = ctypes.string_at(
            ctypes.byref(pict), ctypes.sizeof(pict)
        )
        return token

    monkeypatch.setitem(cb._GDI_COPY_FUNCS, cb.CF_DSPMETAFILEPICT, fake_copy_mf)
    fake.fail_set_for.add(cb.CF_DSPMETAFILEPICT)

    # _free_saved_entry 直接调 _kernel32.GlobalLock / _gdi32.DeleteMetaFile，
    # 这里把两者替换为基于 fake.handles 的查表实现，避免真实地址访问。
    import ctypes

    class _FakeKernel32Free:
        def GlobalLock(self, handle: int):
            # 返回一个稳定的可读地址：把字节塞进 string_buffer，返回其地址。
            if not hasattr(self, "_bufs"):
                self._bufs = {}
            data = fake.handles.get(handle, b"")
            buf = ctypes.create_string_buffer(data, max(len(data), 1))
            self._bufs[handle] = buf
            return ctypes.addressof(buf)

        def GlobalUnlock(self, handle: int):
            return True

    delete_meta_calls: list[int] = []

    class _FakeGdi32Free:
        def DeleteMetaFile(self, h_mf):
            delete_meta_calls.append(h_mf)
            return True

    monkeypatch.setattr(cb, "_kernel32", _FakeKernel32Free())
    monkeypatch.setattr(cb, "_gdi32", _FakeGdi32Free())

    snapshot = cb.backup_clipboard()
    snapshot.restore()
    assert snapshot.restore_failures == 1
    assert fake.freed  # 失败格式的 HGLOBAL 走了释放路径
    assert delete_meta_calls == [inner_hmf]  # 内层 HMETAFILE 被 DeleteMetaFile


def test_open_clipboard_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """_open_clipboard 前 N 次失败最后成功 (调用方完成 backup)。"""
    # 用真实次数计数器替换 _user32.OpenClipboard。
    original_user32 = cb._user32
    real_open = cb._open_clipboard  # 保留真实 (会走 _user32.OpenClipboard) 实现
    calls = {"n": 0}

    class _FlakyUser32:
        def OpenClipboard(self, owner):
            calls["n"] += 1
            return calls["n"] >= 3  # 第 3 次成功

        def __getattr__(self, name):
            return getattr(original_user32, name)

    # 加快测试：去掉 time.sleep 的实际等待。
    monkeypatch.setattr(cb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cb, "_user32", _FlakyUser32())

    fake = _FakeClipboard({cb.CF_UNICODETEXT: "x".encode("utf-16-le") + b"\x00\x00"})
    _patch_clipboard(monkeypatch, fake)
    # _patch_clipboard 把 _open_clipboard 替换为总是成功的 fake.open，
    # 这里恢复为真实实现，使其走被替换后的 _user32.OpenClipboard 重试路径。
    monkeypatch.setattr(cb, "_open_clipboard", real_open)

    snapshot = cb.backup_clipboard()
    assert calls["n"] == 3
    assert snapshot.formats == [cb.CF_UNICODETEXT]


def test_open_clipboard_retries_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """_open_clipboard 全部失败 -> backup_clipboard 抛 ClipboardBackupError。"""
    original_user32 = cb._user32
    real_open = cb._open_clipboard
    calls = {"n": 0}

    class _AlwaysFailUser32:
        def OpenClipboard(self, owner):
            calls["n"] += 1
            return False

        def __getattr__(self, name):
            return getattr(original_user32, name)

    monkeypatch.setattr(cb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cb, "_user32", _AlwaysFailUser32())

    fake = _FakeClipboard({})
    _patch_clipboard(monkeypatch, fake)
    monkeypatch.setattr(cb, "_open_clipboard", real_open)

    with pytest.raises(cb.ClipboardBackupError, match="OpenClipboard"):
        cb.backup_clipboard()
    # 重试上限 5 次
    assert calls["n"] == 5


def test_restore_uses_retrying_open_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """restore() 与 backup() 共用 _open_clipboard 重试封装：
    restore 时前几次 OpenClipboard 失败最终成功也能完成还原。"""
    original_user32 = cb._user32
    real_open = cb._open_clipboard
    calls = {"n": 0}

    class _FlakyUser32:
        def OpenClipboard(self, owner):
            calls["n"] += 1
            return calls["n"] >= 2

        def __getattr__(self, name):
            return getattr(original_user32, name)

    monkeypatch.setattr(cb.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cb, "_user32", _FlakyUser32())

    fake = _FakeClipboard({cb.CF_UNICODETEXT: "hi".encode("utf-16-le") + b"\x00\x00"})
    _patch_clipboard(monkeypatch, fake)
    monkeypatch.setattr(cb, "_open_clipboard", real_open)

    snapshot = cb.backup_clipboard()
    calls["n"] = 0  # 重置计数 restore 阶段
    snapshot.restore()
    assert calls["n"] == 2  # restore 也走了重试
    assert snapshot.restore_failures == 0


# ==================== capture 辅助 ====================


class _SeqUser32:
    """capture._user32 替身：只提供 GetClipboardSequenceNumber。"""

    def __init__(self) -> None:
        self.seq = 100

    def GetClipboardSequenceNumber(self) -> int:
        return self.seq


class _FakeSnapshot:
    """capture.backup_clipboard 替身：记录 restore 调用次数。"""

    def __init__(self) -> None:
        self.restore_calls = 0
        self.restore_exc: Exception | None = None

    def restore(self) -> None:
        self.restore_calls += 1
        if self.restore_exc is not None:
            raise self.restore_exc


def _patch_capture_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    foreground_pid: int,
    text: str | None,
    change_seq_on_copy: bool = True,
    copy_exc: Exception | None = None,
) -> tuple[_SeqUser32, _FakeSnapshot]:
    """布置 capture 的全部外部依赖，返回 (seq_fake, snapshot_fake)。"""
    seq = _SeqUser32()
    snapshot = _FakeSnapshot()
    monkeypatch.setattr(capture, "_user32", seq)
    monkeypatch.setattr(capture, "_foreground_pid", lambda: foreground_pid)
    monkeypatch.setattr(capture, "backup_clipboard", lambda: snapshot)
    monkeypatch.setattr(capture, "get_clipboard_text", lambda: text)

    def fake_copy() -> None:
        if copy_exc is not None:
            raise copy_exc
        if change_seq_on_copy:
            seq.seq += 1

    monkeypatch.setattr(capture, "simulate_copy", fake_copy)
    return seq, snapshot


# ==================== capture ====================


def test_capture_skipped_self(monkeypatch: pytest.MonkeyPatch) -> None:
    """前台窗口属于本进程 -> skipped_self，且不触碰剪贴板。"""
    called = []

    def backup_should_not_run():
        called.append(True)
        raise AssertionError("本进程守卫命中时不应备份剪贴板")

    monkeypatch.setattr(capture, "backup_clipboard", backup_should_not_run)
    monkeypatch.setattr(capture, "_foreground_pid", lambda: os.getpid())
    monkeypatch.setattr(capture, "_user32", _SeqUser32())

    result = capture.capture_selection()
    assert result.status is CaptureStatus.SKIPPED_SELF
    assert not result.ok
    assert called == []


def test_capture_no_sequence_change_no_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    """序列号未变 (复制失败/无选区) -> no_text，且未执行还原。"""
    _, snapshot = _patch_capture_env(
        monkeypatch, foreground_pid=999999, text=None, change_seq_on_copy=False
    )
    result = capture.capture_selection(wait_timeout_s=0.05, poll_interval_s=0.01)
    assert result.status is CaptureStatus.NO_TEXT
    assert snapshot.restore_calls == 0


def test_capture_no_text_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    """剪贴板无文本格式 -> no_text，且执行了还原。"""
    _, snapshot = _patch_capture_env(monkeypatch, foreground_pid=999999, text=None)
    result = capture.capture_selection()
    assert result.status is CaptureStatus.NO_TEXT
    assert snapshot.restore_calls == 1


def test_capture_whitespace_text_is_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 后为空的文本按 no_text 处理。"""
    _, snapshot = _patch_capture_env(monkeypatch, foreground_pid=999999, text="  \n\t ")
    result = capture.capture_selection()
    assert result.status is CaptureStatus.NO_TEXT
    assert snapshot.restore_calls == 1


def test_capture_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    """文本超过 max_chars -> too_long，携带实际长度，且执行了还原。"""
    text = "x" * 3000
    _, snapshot = _patch_capture_env(monkeypatch, foreground_pid=999999, text=text)
    result = capture.capture_selection(max_chars=2000)
    assert result.status is CaptureStatus.TOO_LONG
    assert result.length == 3000
    assert result.text is None
    assert snapshot.restore_calls == 1


def test_capture_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常文本 -> success，文本正确，且执行了还原。"""
    _, snapshot = _patch_capture_env(
        monkeypatch, foreground_pid=999999, text="划词翻译 hello"
    )
    result = capture.capture_selection()
    assert result.status is CaptureStatus.SUCCESS
    assert result.ok
    assert result.text == "划词翻译 hello"
    assert snapshot.restore_calls == 1


def test_capture_copy_exception_still_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    """simulate_copy 抛错 -> error 结果，且异常路径完成还原，不向外抛。"""
    _, snapshot = _patch_capture_env(
        monkeypatch,
        foreground_pid=999999,
        text=None,
        copy_exc=RuntimeError("模拟 SendInput 失败"),
    )
    result = capture.capture_selection()
    assert result.status is CaptureStatus.ERROR
    assert "模拟 SendInput 失败" in result.reason
    assert snapshot.restore_calls == 1


def test_capture_restore_failure_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常路径还原失败 -> error 结果。"""
    _, snapshot = _patch_capture_env(
        monkeypatch, foreground_pid=999999, text="hello"
    )
    snapshot.restore_exc = cb.ClipboardBackupError("模拟还原失败")
    result = capture.capture_selection()
    assert result.status is CaptureStatus.ERROR
    assert "还原" in result.reason


def test_capture_backup_failure_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """备份失败 -> error 结果，不向外抛。"""
    monkeypatch.setattr(capture, "_foreground_pid", lambda: 999999)
    monkeypatch.setattr(capture, "_user32", _SeqUser32())

    def backup_fail():
        raise cb.ClipboardBackupError("模拟 OpenClipboard 失败")

    monkeypatch.setattr(capture, "backup_clipboard", backup_fail)
    result = capture.capture_selection()
    assert result.status is CaptureStatus.ERROR
    assert "OpenClipboard" in result.reason


# ==================== SelectionService ====================


def _make_service(
    on_result=None,
    capture_func=None,
    drag_threshold_px: float = 10.0,
    cooldown_s: float = 0.8,
) -> SelectionService:
    return SelectionService(
        on_result=on_result or (lambda r: None),
        capture_func=capture_func,
        drag_threshold_px=drag_threshold_px,
        cooldown_s=cooldown_s,
    )


def _drag(svc: SelectionService, x1: int, y1: int, x2: int, y2: int) -> None:
    """直接驱动一次左键按下 -> 松开 (绕过 pynput 真实监听)。"""
    svc._on_click(x1, y1, mouse.Button.left, True)
    svc._on_click(x2, y2, mouse.Button.left, False)


def test_drag_below_threshold_not_triggered() -> None:
    """拖拽距离不足阈值不触发。"""
    svc = _make_service(drag_threshold_px=10.0)
    _drag(svc, 100, 100, 105, 103)  # 距离 ~5.8 < 10
    with pytest.raises(queue.Empty):
        svc._queue.get_nowait()


def test_drag_above_threshold_triggered() -> None:
    """拖拽距离超过阈值触发，事件携带松开位置。"""
    svc = _make_service(drag_threshold_px=10.0)
    _drag(svc, 100, 100, 130, 100)  # 距离 30 > 10
    event = svc._queue.get_nowait()
    assert (event.x, event.y) == (130, 100)


def test_drag_right_to_left_triggered() -> None:
    """从右往左划选回归：press(200,100) -> release(40,100)，
    距离 160 超阈值必须触发（距离判定各向同性）。"""
    svc = _make_service(drag_threshold_px=10.0)
    _drag(svc, 200, 100, 40, 100)
    event = svc._queue.get_nowait()
    assert (event.x, event.y) == (40, 100)


def test_cooldown_suppresses_repeat_trigger() -> None:
    """冷却期内重复划选被抑制。"""
    svc = _make_service(drag_threshold_px=10.0, cooldown_s=60.0)
    _drag(svc, 100, 100, 130, 100)
    _drag(svc, 200, 200, 240, 200)  # 冷却期内
    assert svc._queue.qsize() == 1


def test_zero_cooldown_allows_repeat_trigger() -> None:
    """冷却期为 0 时连续划选都触发。"""
    svc = _make_service(drag_threshold_px=10.0, cooldown_s=0.0)
    _drag(svc, 100, 100, 130, 100)
    _drag(svc, 200, 200, 240, 200)
    assert svc._queue.qsize() == 2


def test_release_without_press_ignored() -> None:
    """没有对应按下的松开事件被忽略。"""
    svc = _make_service()
    svc._on_click(500, 500, mouse.Button.left, False)
    with pytest.raises(queue.Empty):
        svc._queue.get_nowait()


def test_right_button_ignored() -> None:
    """非左键拖拽不触发。"""
    svc = _make_service(drag_threshold_px=1.0)
    svc._on_click(100, 100, mouse.Button.right, True)
    svc._on_click(300, 300, mouse.Button.right, False)
    with pytest.raises(queue.Empty):
        svc._queue.get_nowait()


def test_two_drags_share_same_capture_path() -> None:
    """连续两次划选走同一队列 -> 同一 capture -> 同一 on_result。"""
    from dictashift.selection.capture import CaptureResult

    results: list[CaptureResult] = []
    capture_calls: list[int] = []

    def fake_capture() -> CaptureResult:
        capture_calls.append(len(capture_calls))
        return CaptureResult(CaptureStatus.SUCCESS, text=f"t{len(capture_calls)}")

    svc = _make_service(
        on_result=results.append,
        capture_func=fake_capture,
        drag_threshold_px=10.0,
        cooldown_s=0.0,
    )

    class _FakeListener:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout=None) -> None:
            pass

    svc._create_listener = lambda: _FakeListener()  # type: ignore[method-assign]
    svc.start()
    try:
        _drag(svc, 100, 100, 140, 100)
        _drag(svc, 200, 200, 240, 200)
        svc._queue.join()  # 等消费线程处理完两个事件
    finally:
        svc.stop()

    assert len(capture_calls) == 2
    assert [r.text for r in results] == ["t1", "t2"]


def test_double_start_raises_and_stop_idempotent() -> None:
    """重复 start 抛 RuntimeError；stop 幂等。"""
    svc = _make_service()

    class _FakeListener:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout=None) -> None:
            pass

    svc._create_listener = lambda: _FakeListener()  # type: ignore[method-assign]
    svc.stop()  # 未启动时安全调用
    svc.start()
    try:
        with pytest.raises(RuntimeError):
            svc.start()
    finally:
        svc.stop()
    svc.stop()  # 已停止后再次调用安全


def test_capture_exception_does_not_kill_consumer() -> None:
    """capture_func 抛异常：消费线程存活，后续事件照常处理。"""
    from dictashift.selection.capture import CaptureResult

    results: list[CaptureResult] = []
    calls: list[int] = []

    def flaky_capture() -> CaptureResult:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("模拟取词异常")
        return CaptureResult(CaptureStatus.SUCCESS, text="ok")

    svc = _make_service(
        on_result=results.append, capture_func=flaky_capture, cooldown_s=0.0
    )

    class _FakeListener:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout=None) -> None:
            pass

    svc._create_listener = lambda: _FakeListener()  # type: ignore[method-assign]
    svc.start()
    try:
        _drag(svc, 100, 100, 140, 100)
        _drag(svc, 200, 200, 240, 200)
        svc._queue.join()
    finally:
        svc.stop()

    assert len(calls) == 2
    assert [r.text for r in results] == ["ok"]


# ==================== SelectionService.set_auto_trigger ====================


def test_auto_trigger_disabled_ignores_drag() -> None:
    """set_auto_trigger(False) 后划选事件在 _on_click 入口即被丢弃。"""
    svc = _make_service(drag_threshold_px=10.0, cooldown_s=0.0)
    svc.set_auto_trigger(False)
    _drag(svc, 100, 100, 140, 100)
    with pytest.raises(queue.Empty):
        svc._queue.get_nowait()


def test_auto_trigger_reenable_restores_drag() -> None:
    """重新开启后划选恢复触发。"""
    svc = _make_service(drag_threshold_px=10.0, cooldown_s=0.0)
    svc.set_auto_trigger(False)
    svc.set_auto_trigger(True)
    _drag(svc, 100, 100, 140, 100)
    event = svc._queue.get_nowait()
    assert (event.x, event.y) == (140, 100)


def test_auto_trigger_disable_clears_pending_press() -> None:
    """关闭瞬间丢弃残留按下位置：按下 -> 关闭 -> 开启 -> 松开 不构成一次划选。"""
    svc = _make_service(drag_threshold_px=10.0, cooldown_s=0.0)
    svc._on_click(100, 100, mouse.Button.left, True)  # 按下（auto 开启）
    svc.set_auto_trigger(False)  # 关闭 -> 清 press_pos
    svc.set_auto_trigger(True)  # 再开启
    svc._on_click(140, 100, mouse.Button.left, False)  # 松开：无配对按下
    with pytest.raises(queue.Empty):
        svc._queue.get_nowait()


# ==================== SelectionService.on_press_activity ====================


def test_press_activity_fired_on_left_press_only() -> None:
    """on_press_activity：左键按下触发一次；松开与右键不触发。"""
    calls: list[int] = []
    svc = SelectionService(
        on_result=lambda r: None,
        on_press_activity=lambda: calls.append(1),
        cooldown_s=0.0,
    )
    svc._on_click(100, 100, mouse.Button.left, True)
    svc._on_click(100, 100, mouse.Button.left, False)
    svc._on_click(100, 100, mouse.Button.right, True)
    assert calls == [1]


def test_press_activity_fired_even_when_auto_trigger_disabled() -> None:
    """auto_trigger 关闭时按下活动通知仍触发（供装配层收起旧弹窗）。"""
    calls: list[int] = []
    svc = SelectionService(
        on_result=lambda r: None,
        on_press_activity=lambda: calls.append(1),
    )
    svc.set_auto_trigger(False)
    svc._on_click(100, 100, mouse.Button.left, True)
    assert calls == [1]


def test_press_activity_exception_does_not_break_click() -> None:
    """on_press_activity 抛异常：不影响后续划选判定（按下位置仍被记录）。"""
    def boom() -> None:
        raise RuntimeError("模拟活动回调异常")

    svc = SelectionService(
        on_result=lambda r: None,
        on_press_activity=boom,
        drag_threshold_px=10.0,
    )
    _drag(svc, 100, 100, 140, 100)
    event = svc._queue.get_nowait()
    assert (event.x, event.y) == (140, 100)
