"""model_download 测试：用伪 httpx.Client 构造内存 zip，不真实下载。

测试目标 (AGENTS.md 13.1)：
- 正常路径：内存 zip 含三个文件（大小匹配）+ 一个额外文件（被忽略）+ 一个
  路径穿越恶意条目（被忽略）-> 解压出三个文件、大小匹配、progress_cb 被调用。
- total 未知（无 Content-Length）-> progress_cb 收到 total=-1。
- 大小校验失败 -> ModelDownloadError，并清理半成品（已解压文件被删）。
- HTTP 非 200 -> ModelDownloadError。
- 缺少模型文件的 zip -> ModelDownloadError。
- 路径穿越条目（base 名匹配必需文件名）-> 拒绝 + 清理。
- 临时 zip 文件下载后清理。
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager

import pytest

from dictashift.asr import model_download as md
from dictashift.asr.providers import REQUIRED_MODEL_FILES


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _good_zip_bytes() -> bytes:
    entries: dict[str, bytes] = {}
    for name in REQUIRED_MODEL_FILES:
        entries[name] = b"a" * md.EXPECTED_SIZES[name]
    entries["extra/README.txt"] = b"hello"  # 额外文件，应被忽略
    entries["evil/../../../evil.txt"] = b"x"  # 穿越条目，base 名不匹配，被忽略
    return _make_zip(entries)


class _FakeStreamResponse:
    """模拟 httpx stream GET 的响应。"""

    def __init__(self, status: int, body: bytes, content_length: int | None):
        self.status_code = status
        self._body = body
        # headers 行为：content_length=None 时不含 Content-Length 键。
        self.headers = (
            {"Content-Length": str(content_length)} if content_length is not None else {}
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def iter_bytes(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _FakeClient:
    """替代 httpx.Client：构造可控的流式响应。"""

    def __init__(self, response: _FakeStreamResponse):
        self._response = response
        self.headers = {}  # Client 级 headers（构造参数）

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    @contextmanager
    def stream(self, method, url):
        yield self._response


@pytest.fixture
def patch_httpx(monkeypatch):
    """返回一个工厂：传入 (status, body, content_length) 注入伪 Client。"""

    def _install(status: int, body: bytes, content_length: int | None) -> _FakeClient:
        resp = _FakeStreamResponse(status, body, content_length)
        client = _FakeClient(resp)
        monkeypatch.setattr(md.httpx, "Client", lambda *a, **kw: client)
        return client

    return _install


# ==================== 正常下载 ====================


def test_download_success_extracts_three_files(tmp_path, patch_httpx) -> None:
    body = _good_zip_bytes()
    patch_httpx(200, body, content_length=len(body))

    progress = []
    md.download_models(tmp_path, progress_cb=lambda d, t: progress.append((d, t)))

    for name in REQUIRED_MODEL_FILES:
        f = tmp_path / name
        assert f.exists()
        assert f.stat().st_size == md.EXPECTED_SIZES[name]
    # 额外文件未被解压
    assert not (tmp_path / "README.txt").exists()
    # 恶意穿越条目未被解压
    assert not (tmp_path.parent.parent.parent / "evil.txt").exists()
    # progress_cb 被调用，total 正确
    assert progress
    assert progress[0][1] == len(body)
    assert progress[-1][0] == len(body)


def test_download_progress_total_unknown_when_no_content_length(
    tmp_path, patch_httpx
) -> None:
    """无 Content-Length 时 total 传 -1。"""
    body = _good_zip_bytes()
    patch_httpx(200, body, content_length=None)

    progress = []
    md.download_models(tmp_path, progress_cb=lambda d, t: progress.append((d, t)))

    assert progress
    assert all(t == -1 for _, t in progress)


# ==================== 大小校验失败 -> 清理半成品 ====================


def test_download_size_mismatch_cleans_up_and_raises(tmp_path, patch_httpx) -> None:
    entries = {name: b"a" * md.EXPECTED_SIZES[name] for name in REQUIRED_MODEL_FILES}
    entries[REQUIRED_MODEL_FILES[0]] = b"a" * (md.EXPECTED_SIZES[REQUIRED_MODEL_FILES[0]] - 1)
    body = _make_zip(entries)
    patch_httpx(200, body, content_length=len(body))

    with pytest.raises(md.ModelDownloadError):
        md.download_models(tmp_path)

    # 半成品已清理。
    for name in REQUIRED_MODEL_FILES:
        assert not (tmp_path / name).exists()


# ==================== HTTP 非 200 ====================


def test_download_http_error_raises(tmp_path, patch_httpx) -> None:
    patch_httpx(404, b"not found", content_length=9)

    with pytest.raises(md.ModelDownloadError) as ei:
        md.download_models(tmp_path)
    assert "404" in str(ei.value)

    for name in REQUIRED_MODEL_FILES:
        assert not (tmp_path / name).exists()


# ==================== zip 缺少模型文件 ====================


def test_download_zip_missing_model_file_raises(tmp_path, patch_httpx) -> None:
    entries = {name: b"a" * md.EXPECTED_SIZES[name] for name in REQUIRED_MODEL_FILES}
    del entries[REQUIRED_MODEL_FILES[1]]
    body = _make_zip(entries)
    patch_httpx(200, body, content_length=len(body))

    with pytest.raises(md.ModelDownloadError):
        md.download_models(tmp_path)

    for name in REQUIRED_MODEL_FILES:
        assert not (tmp_path / name).exists()


# ==================== 路径穿越条目（base 名匹配）被拒绝 ====================


def test_download_rejects_traversal_when_matches_required_name(
    tmp_path, patch_httpx
) -> None:
    entries: dict[str, bytes] = {}
    # 第一个必需文件以穿越路径形式出现（base 名匹配，但路径含 ..）。
    evil = "../../../" + REQUIRED_MODEL_FILES[0]
    entries[evil] = b"a" * md.EXPECTED_SIZES[REQUIRED_MODEL_FILES[0]]
    for name in REQUIRED_MODEL_FILES[1:]:
        entries[name] = b"a" * md.EXPECTED_SIZES[name]
    body = _make_zip(entries)
    patch_httpx(200, body, content_length=len(body))

    with pytest.raises(md.ModelDownloadError):
        md.download_models(tmp_path)

    # 穿越目标文件不应存在；已解压的合法文件也清理。
    assert not (tmp_path.parent.parent.parent / REQUIRED_MODEL_FILES[0]).exists()
    for name in REQUIRED_MODEL_FILES[1:]:
        assert not (tmp_path / name).exists()


# ==================== 临时文件清理 ====================


def test_download_cleans_temp_zip_on_success(tmp_path, patch_httpx, monkeypatch) -> None:
    body = _good_zip_bytes()
    patch_httpx(200, body, content_length=len(body))

    created = []
    orig_mktemp = md.tempfile.mktemp

    def _spy(*a, **kw):
        p = orig_mktemp(*a, **kw)
        created.append(p)
        return p

    monkeypatch.setattr(md.tempfile, "mktemp", _spy)
    md.download_models(tmp_path)

    import os

    for p in created:
        assert not os.path.exists(p)


# ==================== 错误消息不含响应头/不含敏感信息 ====================


def test_error_message_does_not_leak_response_headers(tmp_path, patch_httpx) -> None:
    """HTTP 错误消息可读、不含响应头内容。"""
    body = _good_zip_bytes()
    patch_httpx(500, body, content_length=len(body))

    with pytest.raises(md.ModelDownloadError) as ei:
        md.download_models(tmp_path)
    msg = str(ei.value)
    assert "500" in msg
    # 不应包含响应头键名。
    assert "Content-Type" not in msg
