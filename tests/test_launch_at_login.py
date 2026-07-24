"""launch_at_login 测试：monkeypatch winreg，不真实写注册表。

测试目标 (AGENTS.md 13.1)：
- is_launch_at_login_enabled：值存在且非空 -> True；缺失/空 -> False。
- set_launch_at_login(True)：
  * 打包模式（sys.frozen 真）-> 命令为带引号的 sys.executable。
  * 开发模式 -> pythonw.exe -m dictashift（找不到 pythonw 退回 python.exe）。
- set_launch_at_login(False)：删除值；值不存在不报错。
- 写入/删除失败抛 OSError。
"""

from __future__ import annotations

import sys

import pytest

from dictashift.settings import launch_at_login as lal_mod


# ==================== fake winreg ====================


class _FakeKey:
    """模拟注册表键：内存 dict 存值（由模块级函数操作）。

    OpenKey/CreateKey 返回 _FakeKey 实例（上下文管理器）。值的实际读写由
    模块级 _query_value_ex / _set_value_ex / _delete_value_ex 完成，与真实
    winreg 的 API 形状一致（函数接收 key 作为第一参数）。
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *exc) -> None:
        return None


# 内存 store：键路径 -> {值名: (值, 类型)}。
_store: dict[str, dict[str, tuple]] = {}
# 全局开关：写入/删除失败（测试 OSError 路径）。
_write_fails = False


def _ensure(path: str) -> dict[str, tuple]:
    return _store.setdefault(path, {})


def _open_key(hkey, path, *args):
    return _FakeKey(path)


def _create_key(hkey, path):
    return _FakeKey(path)


def _query_value_ex(key, name):
    d = _ensure(key.path)
    if name not in d:
        raise FileNotFoundError(name)
    return d[name]


def _set_value_ex(key, name, _reserved, type_, value):
    if _write_fails:
        raise OSError("fake write failure")
    _ensure(key.path)[name] = (value, type_)


def _delete_value(key, name):
    if _write_fails:
        raise OSError("fake delete failure")
    d = _ensure(key.path)
    if name not in d:
        raise FileNotFoundError(name)
    del d[name]


@pytest.fixture
def fake_winreg(monkeypatch):
    """替换 launch_at_login 模块内的 winreg，并重置内存 store。"""
    _store.clear()
    global _write_fails
    _write_fails = False
    # 预置 RUN 键的 dict，方便测试直接 _store[_RUN_KEY][...] 赋值。
    _ensure(lal_mod._RUN_KEY)
    monkeypatch.setattr(lal_mod.winreg, "HKEY_CURRENT_USER", 0x80000001)
    monkeypatch.setattr(lal_mod.winreg, "OpenKey", _open_key)
    monkeypatch.setattr(lal_mod.winreg, "CreateKey", _create_key)
    monkeypatch.setattr(lal_mod.winreg, "QueryValueEx", _query_value_ex)
    monkeypatch.setattr(lal_mod.winreg, "SetValueEx", _set_value_ex)
    monkeypatch.setattr(lal_mod.winreg, "DeleteValue", _delete_value)
    monkeypatch.setattr(lal_mod.winreg, "REG_SZ", 1)
    return _store


def _store_value() -> tuple | None:
    return _store.get(lal_mod._RUN_KEY, {}).get(lal_mod._VALUE_NAME)


# ==================== is_enabled ====================


def test_is_enabled_false_when_missing(fake_winreg) -> None:
    assert lal_mod.is_launch_at_login_enabled() is False


def test_is_enabled_true_when_value_nonempty(fake_winreg) -> None:
    _store[lal_mod._RUN_KEY][lal_mod._VALUE_NAME] = ("some cmd", 1)
    assert lal_mod.is_launch_at_login_enabled() is True


def test_is_enabled_false_when_empty_string(fake_winreg) -> None:
    _store[lal_mod._RUN_KEY][lal_mod._VALUE_NAME] = ("", 1)
    assert lal_mod.is_launch_at_login_enabled() is False


# ==================== set True: frozen 模式 ====================


def test_set_true_frozen_uses_executable(fake_winreg, monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Program Files/DictaShift/DictaShift.exe")
    lal_mod.set_launch_at_login(True)
    val = _store_value()
    assert val is not None
    cmd = val[0]
    # Path 会把斜杠规范化为当前平台分隔符；只断言结构与可执行文件名。
    assert cmd.startswith('"') and cmd.endswith('"')
    assert "DictaShift.exe" in cmd
    assert "-m dictashift" not in cmd


# ==================== set True: 开发模式 ====================


def test_set_true_dev_mode_prefers_pythonw(fake_winreg, monkeypatch, tmp_path) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    # 造一个 pythonw.exe 同目录环境。
    py_dir = tmp_path / "venv" / "Scripts"
    py_dir.mkdir(parents=True)
    (py_dir / "pythonw.exe").write_text("x")
    (py_dir / "python.exe").write_text("x")
    exe = str(py_dir / "python.exe").replace("/", "\\")
    monkeypatch.setattr(sys, "executable", exe)
    lal_mod.set_launch_at_login(True)
    cmd = _store_value()[0]
    assert "pythonw.exe" in cmd
    assert "-m dictashift" in cmd
    # 命令形如 "<pythonw 路径>" -m dictashift，开头带引号即可。
    assert cmd.startswith('"')


def test_set_true_dev_mode_falls_back_to_python_exe(
    fake_winreg, monkeypatch, tmp_path
) -> None:
    """sys.executable 同目录无 pythonw.exe -> 退回 python.exe -m dictashift。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    py_dir = tmp_path / "venv2" / "Scripts"
    py_dir.mkdir(parents=True)
    # 只有 python.exe，无 pythonw.exe
    (py_dir / "python.exe").write_text("x")
    exe = str(py_dir / "python.exe").replace("/", "\\")
    monkeypatch.setattr(sys, "executable", exe)
    lal_mod.set_launch_at_login(True)
    cmd = _store_value()[0]
    assert "python.exe" in cmd
    assert "pythonw.exe" not in cmd
    assert "-m dictashift" in cmd


# ==================== set False ====================


def test_set_false_deletes_value(fake_winreg, monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    _store[lal_mod._RUN_KEY][lal_mod._VALUE_NAME] = ("cmd", 1)
    lal_mod.set_launch_at_login(False)
    assert _store_value() is None


def test_set_false_missing_value_is_noop(fake_winreg, monkeypatch) -> None:
    """值不存在时删除不报错。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    # 不应抛
    lal_mod.set_launch_at_login(False)
    assert _store_value() is None


# ==================== OSError ====================


def test_set_true_raises_on_write_failure(fake_winreg, monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    global _write_fails
    _write_fails = True
    with pytest.raises(OSError):
        lal_mod.set_launch_at_login(True)


def test_set_false_raises_on_delete_failure(fake_winreg, monkeypatch) -> None:
    """值存在但删除失败 -> OSError（值不存在才幂等）。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    _store[lal_mod._RUN_KEY][lal_mod._VALUE_NAME] = ("cmd", 1)
    global _write_fails
    _write_fails = True
    with pytest.raises(OSError):
        lal_mod.set_launch_at_login(False)
