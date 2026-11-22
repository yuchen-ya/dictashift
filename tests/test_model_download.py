"""model_download 测试：用伪 httpx.Client 构造内存 zip，不真实下载。

测试目标：
- 正常路径：内存 zip 含三个文件（大小匹配）+ 一个额外文件（被忽略）+ 一个
  路径穿越恶意条目（被忽略）-> 解压出三个文件、大小匹配、progress_cb 被调用。
- total 未知（无 Content-Length）-> progress_cb 收到 total=-1。
- 大小/SHA256 校验失败 -> ModelDownloadError，临时目录清理。
- HTTP 非 200 -> ModelDownloadError。
- 缺少模型文件的 zip -> ModelDownloadError。
- 路径穿越条目（base 名匹配必需文件名）-> 拒绝。
- 临时文件/临时目录下载后清理。
- 两阶段安装（本次修复）：
  * 损坏下载（哈希/尺寸不符）保留正式目录里的既有模型，可恢复（坏包后好包能装上）；
  * 成功安装只替换三个模型文件，目录里其它文件不动；
  * 临时目录建在 dest 的兄弟目录（同盘，os.replace 原子）；
  * SHA256 分块计算（不整读大文件）。
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


def _good_hashes() -> dict[str, str]:
    """与 _good_zip_bytes 的假内容匹配的 SHA256（成功路径测试注入用）。"""
    import hashlib

    return {
        name: hashlib.sha256(b"a" * md.EXPECTED_SIZES[name]).hexdigest()
        for name in REQUIRED_MODEL_FILES
    }


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
    md.download_models(
        tmp_path,
        progress_cb=lambda d, t: progress.append((d, t)),
        expected_hashes=_good_hashes(),
    )

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
    md.download_models(
        tmp_path,
        progress_cb=lambda d, t: progress.append((d, t)),
        expected_hashes=_good_hashes(),
    )

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


def test_download_hash_mismatch_cleans_up_and_raises(tmp_path, patch_httpx) -> None:
    """SHA256 不匹配（内容被篡改但大小相同）-> 抛错并清理半成品。"""
    body = _good_zip_bytes()
    patch_httpx(200, body, content_length=len(body))

    bad_hashes = _good_hashes()
    bad_hashes[REQUIRED_MODEL_FILES[0]] = "0" * 64  # 故意不匹配的哈希

    with pytest.raises(md.ModelDownloadError, match="SHA256"):
        md.download_models(tmp_path, expected_hashes=bad_hashes)

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
    """临时 zip 与整个临时目录在成功后都不留痕迹。"""
    body = _good_zip_bytes()
    patch_httpx(200, body, content_length=len(body))

    dest = tmp_path / "models" / "sensevoice_onnx"
    created = []
    orig_mkstemp = md.tempfile.mkstemp

    def _spy(*a, **kw):
        fd, p = orig_mkstemp(*a, **kw)
        created.append(p)
        return fd, p

    monkeypatch.setattr(md.tempfile, "mkstemp", _spy)
    md.download_models(dest, expected_hashes=_good_hashes())

    for p in created:
        assert not Path(p).exists()
    # 临时目录（dest 的兄弟目录）已清理：父目录里只剩 dest 自身。
    assert list(dest.parent.iterdir()) == [dest]


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


# ==================== 两阶段安装：失败保留既有模型 ====================


def _install_fake_models(dest: Path) -> dict[str, bytes]:
    """在 dest 预置「旧模型」：与期望尺寸同长但内容不同的假文件。"""
    old: dict[str, bytes] = {}
    dest.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_MODEL_FILES:
        data = b"b" * md.EXPECTED_SIZES[name]
        (dest / name).write_bytes(data)
        old[name] = data
    return old


def _corrupt_zip_bytes() -> bytes:
    """尺寸匹配但内容被篡改的 zip（触发 SHA256 失败）。"""
    entries = {name: b"b" * md.EXPECTED_SIZES[name] for name in REQUIRED_MODEL_FILES}
    return _make_zip(entries)


def test_corrupt_download_preserves_existing_models(tmp_path, patch_httpx) -> None:
    """损坏下载（哈希不匹配）不得破坏正式目录里的既有模型。"""
    dest = tmp_path / "models" / "sensevoice_onnx"
    old = _install_fake_models(dest)
    body = _corrupt_zip_bytes()
    patch_httpx(200, body, content_length=len(body))

    with pytest.raises(md.ModelDownloadError, match="SHA256"):
        md.download_models(dest)

    # 旧模型逐字节未变。
    for name, data in old.items():
        assert (dest / name).read_bytes() == data
    # 临时目录已清理，父目录不留残留。
    assert list(dest.parent.iterdir()) == [dest]


def test_failed_download_is_recoverable(tmp_path, patch_httpx) -> None:
    """失败后可直接重试：同一次会话内坏包 -> 好包能正常装上。"""
    dest = tmp_path / "models" / "sensevoice_onnx"
    old = _install_fake_models(dest)

    # 第一次：坏包失败，旧模型保留。
    patch_httpx(200, _corrupt_zip_bytes(), content_length=None)
    with pytest.raises(md.ModelDownloadError):
        md.download_models(dest)
    for name, data in old.items():
        assert (dest / name).read_bytes() == data

    # 第二次：好包成功，新内容替换旧内容。
    good = _good_zip_bytes()
    patch_httpx(200, good, content_length=len(good))
    md.download_models(dest, expected_hashes=_good_hashes())
    for name in REQUIRED_MODEL_FILES:
        assert (dest / name).read_bytes() == b"a" * md.EXPECTED_SIZES[name]


def test_success_replaces_models_and_keeps_other_files(tmp_path, patch_httpx) -> None:
    """成功安装只替换三个模型文件，目录里其它文件保持不动。"""
    dest = tmp_path / "models" / "sensevoice_onnx"
    _install_fake_models(dest)
    (dest / "notes.txt").write_bytes(b"keep me")

    body = _good_zip_bytes()
    patch_httpx(200, body, content_length=len(body))
    md.download_models(dest, expected_hashes=_good_hashes())

    assert (dest / "notes.txt").read_bytes() == b"keep me"
    for name in REQUIRED_MODEL_FILES:
        assert (dest / name).stat().st_size == md.EXPECTED_SIZES[name]


def test_failed_replacement_restores_previous_model_set(tmp_path, patch_httpx, monkeypatch):
    dest = tmp_path / "models" / "sensevoice_onnx"
    old = _install_fake_models(dest)
    good = _good_zip_bytes()
    patch_httpx(200, good, content_length=len(good))
    replace = md.os.replace

    def fail_second_new_model(source, target):
        if source.parent.name.startswith(".sensevoice_onnx.tmp-") and source.name == REQUIRED_MODEL_FILES[1]:
            raise PermissionError("model file locked")
        replace(source, target)

    monkeypatch.setattr(md.os, "replace", fail_second_new_model)
    with pytest.raises(md.ModelDownloadError, match="模型安装失败"):
        md.download_models(dest, expected_hashes=_good_hashes())

    assert {name: (dest / name).read_bytes() for name in old} == old
    assert list(dest.parent.iterdir()) == [dest]


def test_size_mismatch_failure_preserves_existing_models(tmp_path, patch_httpx) -> None:
    """尺寸不匹配失败同样不碰既有模型（另一条失败路径）。"""
    dest = tmp_path / "m" / "sensevoice_onnx"
    old = _install_fake_models(dest)

    entries = {name: b"a" * md.EXPECTED_SIZES[name] for name in REQUIRED_MODEL_FILES}
    entries[REQUIRED_MODEL_FILES[2]] = b"a" * (md.EXPECTED_SIZES[REQUIRED_MODEL_FILES[2]] - 1)
    body = _make_zip(entries)
    patch_httpx(200, body, content_length=len(body))

    with pytest.raises(md.ModelDownloadError, match="大小"):
        md.download_models(dest)

    for name, data in old.items():
        assert (dest / name).read_bytes() == data
    assert list(dest.parent.iterdir()) == [dest]
