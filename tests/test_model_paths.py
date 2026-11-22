"""模型路径解析测试：user_model_dir / default_model_dir / models_ready。

测试目标 (AGENTS.md 13.1)：
- user_model_dir：APPDATA 存在时用 %APPDATA%\\DictaShift\\models\\sensevoice_onnx；
  缺失时回退 ~/.dictashift/models/sensevoice_onnx。
- models_ready：三文件齐全 True，缺任一 False。
- default_model_dir 解析顺序：
  1. user_model_dir() 文件齐全 -> 用 user 目录；
  2. 否则仓库 assets 目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dictashift.asr import providers as prov


# ==================== user_model_dir ====================


def test_user_model_dir_uses_appdata(monkeypatch, tmp_path) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    d = prov.user_model_dir()
    assert d == appdata / "DictaShift" / "models" / "sensevoice_onnx"


def test_user_model_dir_fallback_no_appdata(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    d = prov.user_model_dir()
    assert d == tmp_path / ".dictashift" / "models" / "sensevoice_onnx"


# ==================== models_ready ====================


def test_models_ready_true_when_all_present(tmp_path) -> None:
    for name in prov.REQUIRED_MODEL_FILES:
        (tmp_path / name).write_bytes(b"x")
    assert prov.models_ready(tmp_path) is True


def test_models_ready_false_when_missing(tmp_path) -> None:
    # 只放一个文件
    (tmp_path / prov.REQUIRED_MODEL_FILES[0]).write_bytes(b"x")
    assert prov.models_ready(tmp_path) is False


def test_models_ready_false_when_name_is_directory(tmp_path) -> None:
    """同名目录不算就绪（旧版只看 exists 会误判为 True）。"""
    for i, name in enumerate(prov.REQUIRED_MODEL_FILES):
        if i == 0:
            (tmp_path / name).mkdir()  # 目录占位
        else:
            (tmp_path / name).write_bytes(b"x")
    assert prov.models_ready(tmp_path) is False


def test_models_ready_false_when_any_file_empty(tmp_path) -> None:
    """0 字节空文件不算就绪（下载中断的半成品）。"""
    for i, name in enumerate(prov.REQUIRED_MODEL_FILES):
        (tmp_path / name).write_bytes(b"x" if i else b"")
    assert prov.models_ready(tmp_path) is False


def test_models_ready_false_when_empty(tmp_path) -> None:
    assert prov.models_ready(tmp_path) is False


# ==================== default_model_dir 解析 ====================


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """把 user_model_dir 指向 tmp_path/user，仓库 assets 不动。

    返回 (user_dir, repo_assets_dir)。
    """
    user_dir = tmp_path / "user" / "DictaShift" / "models" / "sensevoice_onnx"
    repo_assets = tmp_path / "repo" / "assets" / "models" / "sensevoice_onnx"
    repo_assets.mkdir(parents=True)
    # 让 user_model_dir 返回受控路径。
    monkeypatch.setattr(prov, "user_model_dir", lambda: user_dir)
    monkeypatch.setattr(prov, "_repo_assets_model_dir", lambda: repo_assets)
    return user_dir, repo_assets


def test_default_prefers_user_dir_when_ready(isolated_paths) -> None:
    user_dir, repo_assets = isolated_paths
    user_dir.mkdir(parents=True, exist_ok=True)
    for name in prov.REQUIRED_MODEL_FILES:
        (user_dir / name).write_bytes(b"x")
    # 仓库 assets 也齐全，但应优先 user。
    for name in prov.REQUIRED_MODEL_FILES:
        (repo_assets / name).write_bytes(b"x")
    assert prov.default_model_dir() == user_dir


def test_default_falls_back_to_repo_when_user_incomplete(isolated_paths) -> None:
    user_dir, repo_assets = isolated_paths
    user_dir.mkdir(parents=True, exist_ok=True)
    # user 缺一文件
    (user_dir / prov.REQUIRED_MODEL_FILES[0]).write_bytes(b"x")
    # 仓库齐全
    for name in prov.REQUIRED_MODEL_FILES:
        (repo_assets / name).write_bytes(b"x")
    assert prov.default_model_dir() == repo_assets


def test_default_falls_back_to_repo_when_user_empty(isolated_paths) -> None:
    """user 目录不存在 -> 回退仓库（即便仓库也不齐全，仍返回仓库路径）。"""
    _, repo_assets = isolated_paths
    assert prov.default_model_dir() == repo_assets


# ==================== REQUIRED_MODEL_FILES 常量 ====================


def test_required_model_files_exact() -> None:
    assert set(prov.REQUIRED_MODEL_FILES) == {
        "SenseVoice-Encoder.int8.onnx",
        "SenseVoice-CTC.int8.onnx",
        "Tokenizer.bpe.model",
    }


def test_model_paths_uses_required_names(tmp_path) -> None:
    enc, dec, tok = prov.model_paths(tmp_path)
    assert enc.name == "SenseVoice-Encoder.int8.onnx"
    assert dec.name == "SenseVoice-CTC.int8.onnx"
    assert tok.name == "Tokenizer.bpe.model"
