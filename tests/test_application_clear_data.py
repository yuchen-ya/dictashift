"""DictaShiftApplication「清除全部数据并退出」流程测试。

测试目标 (AGENTS.md 11/12)：
- _on_clear_data：
  * 顺序调用 hotkeys.stop -> controller.shutdown -> asr.release（先 release ONNX 再 rmtree）；
  * 关闭日志 handler（_close_logging_handlers）；
  * shutil.rmtree(app_data_dir()) 删除整个数据目录；
  * 置 _data_cleared=True 并调 app.quit()。
- shutdown：_data_cleared=True 时跳过 repo.save（防止退出流程重建 settings.json）。

复用 test_application_routing 的 __new__ fake 装配模式，避免构建完整应用。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging
from pathlib import Path

import pytest

from dictashift.application import DictaShiftApplication
from dictashift.settings.models import AppSettings


class _FakeHotkeys:
    def __init__(self) -> None:
        self.stop_called = False

    def stop(self) -> None:
        self.stop_called = True


class _FakeController:
    def __init__(self) -> None:
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True


class _FakeAsr:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeApp:
    def __init__(self) -> None:
        self.quit_called = False

    def quit(self) -> None:
        self.quit_called = True


class _FakeRepo:
    def __init__(self) -> None:
        self.save_calls: list = []

    def save(self, settings) -> None:
        self.save_calls.append(settings)


def _make_app() -> tuple[DictaShiftApplication, _FakeHotkeys, _FakeController, _FakeAsr, _FakeApp, _FakeRepo]:
    """跳过 __init__，仅装配 _on_clear_data / shutdown 直接读取的属性。"""
    da = DictaShiftApplication.__new__(DictaShiftApplication)
    da._hotkeys = _FakeHotkeys()
    da._controller = _FakeController()
    da._asr = _FakeAsr()
    da._app = _FakeApp()
    da._repo = _FakeRepo()
    da.settings = AppSettings()
    da._data_cleared = False
    return da, da._hotkeys, da._controller, da._asr, da._app, da._repo


def _make_data_tree(base: Path) -> dict:
    """在 base 下构造 settings.json / logs / models 文件结构，返回路径字典。"""
    settings = base / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    log_file = base / "logs" / "dictashift.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("log", encoding="utf-8")
    model_file = base / "models" / "sensevoice_onnx" / "x.onnx"
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_text("model", encoding="utf-8")
    return {"settings": settings, "log": log_file, "model": model_file, "base": base}


def test_on_clear_data_stops_services_then_removes_dir_then_quits(
    monkeypatch, tmp_path
) -> None:
    """_on_clear_data 顺序：停服务 -> 关日志句柄 -> 删目录 -> _data_cleared -> quit。"""
    da, hotkeys, controller, asr, app, repo = _make_app()
    paths = _make_data_tree(tmp_path)
    monkeypatch.setattr(
        "dictashift.application.app_data_dir", lambda: paths["base"]
    )
    # 避免影响 pytest 日志：monkeypatch 关闭 handler 为空操作。
    monkeypatch.setattr(da, "_close_logging_handlers", lambda: None)

    da._on_clear_data()

    assert hotkeys.stop_called
    assert controller.shutdown_called
    assert asr.released
    # 服务停止被调用（无法严格断言顺序但都调用过）
    assert not paths["base"].exists()
    assert da._data_cleared is True
    assert app.quit_called


def test_on_clear_data_delete_failure_does_not_block_quit(
    monkeypatch, tmp_path
) -> None:
    """rmtree 抛异常也不阻塞：_data_cleared 仍置 True、quit 仍调用。"""
    da, *_ = _make_app()
    base = tmp_path / "data"
    base.mkdir()
    monkeypatch.setattr("dictashift.application.app_data_dir", lambda: base)
    monkeypatch.setattr(da, "_close_logging_handlers", lambda: None)
    import dictashift.application as appmod

    def _boom(*a, **kw):
        raise OSError("denied")

    monkeypatch.setattr(appmod.shutil, "rmtree", _boom)

    da._on_clear_data()

    assert da._data_cleared is True
    assert da._app.quit_called


def test_shutdown_skips_save_after_clear(monkeypatch, tmp_path) -> None:
    """_data_cleared=True 时 shutdown 不调 repo.save。"""
    da, hotkeys, controller, asr, app, repo = _make_app()
    da._hotkey_queue = None  # shutdown 会 put(None)/join，避免真实队列
    da._hotkey_exec_thread = None
    da._data_cleared = True

    # shutdown 访问 _hotkey_queue / _hotkey_exec_thread；用 try 兜底，但更稳妥 patch
    class _NoOp:
        def put(self, *a, **kw):
            pass

        def join(self, *a, **kw):
            pass

    da._hotkey_queue = _NoOp()  # type: ignore[assignment]
    da._hotkey_exec_thread = None  # type: ignore[assignment]

    da.shutdown()
    assert repo.save_calls == []


def test_shutdown_still_saves_when_not_cleared(monkeypatch, tmp_path) -> None:
    """_data_cleared=False 时 shutdown 仍保存设置（行为不变）。"""
    da, *_ = _make_app()
    repo = da._repo

    class _NoOp:
        def put(self, *a, **kw):
            pass

        def join(self, *a, **kw):
            pass

    da._hotkey_queue = _NoOp()  # type: ignore[assignment]
    da._hotkey_exec_thread = None  # type: ignore[assignment]
    da._data_cleared = False

    da.shutdown()
    assert len(repo.save_calls) == 1


def test_close_logging_handlers_closes_and_removes(monkeypatch) -> None:
    """_close_logging_handlers 遍历 root logger，close + remove 所有 handler。"""
    da, *_ = _make_app()
    root = logging.getLogger()
    # 暂时挂上若干 fake handler，避免影响 pytest 真实 handler
    from logging import Handler

    class _FakeHandler(Handler):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def emit(self, record) -> None:  # pragma: no cover
            pass

    h1 = _FakeHandler()
    h2 = _FakeHandler()
    root.addHandler(h1)
    root.addHandler(h2)
    try:
        da._close_logging_handlers()
        assert h1.closed
        assert h2.closed
        # 已被 removeHandler
        assert h1 not in root.handlers
        assert h2 not in root.handlers
    finally:
        # 兜底清理
        for h in (h1, h2):
            if h in root.handlers:
                root.removeHandler(h)
