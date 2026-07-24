"""翻译模块测试：SSE 解析、请求体构造、httpx.MockTransport 端到端、llama_server 常量。

测试目标：
- _parse_sse_line：正常 delta 行、[DONE]、空行、非 data 行、坏 JSON、无 choices、content=None。
- _build_request_body：含 stream=True、chat_template_kwargs.enable_thinking is False、
  stream_options、system+user messages。
- httpx.MockTransport 端到端：多 chunk SSE 流拼接；HTTP 500 -> TranslationError(500)；
  ConnectError -> TranslationError(None)；无 api_key 无 Authorization 头；有 api_key 有头。
- llama_server：DEFAULT_LLAMA_PORT==19120；default_local_profile base_url 正确。

注意：不打印 api_key 值，仅断言 Authorization 头存在且前缀正确。
"""

from __future__ import annotations

import json

import httpx
import pytest

from dictashift.translation import (
    DEFAULT_LLAMA_PORT,
    BUILTIN_BING,
    BUILTIN_GOOGLE,
    LlamaServerBackend,
    OpenAICompatibleBackend,
    TranslationError,
    TranslationProfile,
    builtin_free_profiles,
    builtin_name_for_backend_type,
    builtin_profile_by_name,
    builtin_profile_names,
)
from dictashift.translation.openai_compatible import _build_request_body, _parse_sse_line


# ==================== _parse_sse_line ====================


def test_parse_sse_normal_delta_line() -> None:
    line = 'data: {"choices":[{"delta":{"content":"hello"}}]}'
    assert _parse_sse_line(line) == "hello"


def test_parse_sse_done_returns_none() -> None:
    assert _parse_sse_line("data: [DONE]") is None


def test_parse_sse_empty_line_returns_none() -> None:
    assert _parse_sse_line("") is None


def test_parse_sse_whitespace_line_returns_none() -> None:
    assert _parse_sse_line("   ") is None
    assert _parse_sse_line("\n\t") is None


def test_parse_sse_non_data_line_returns_none() -> None:
    """注释/事件名等非 data: 前缀行返回 None。"""
    assert _parse_sse_line(": heartbeat") is None
    assert _parse_sse_line("event: delta") is None


def test_parse_sse_bad_json_returns_none() -> None:
    """坏 JSON 行忽略返回 None。"""
    assert _parse_sse_line("data: {not json") is None


def test_parse_sse_no_choices_returns_none() -> None:
    line = 'data: {"choices":[]}'
    assert _parse_sse_line(line) is None
    line2 = 'data: {"other":1}'
    assert _parse_sse_line(line2) is None


def test_parse_sse_content_none_returns_none() -> None:
    """content 为 None（如首帧 role 帧）返回 None。"""
    line = 'data: {"choices":[{"delta":{"role":"assistant"}}]}'
    assert _parse_sse_line(line) is None


def test_parse_sse_empty_content_string_returns_empty() -> None:
    """content 为空字符串时返回空串（非 None），与 None 区分。"""
    line = 'data: {"choices":[{"delta":{"content":""}}]}'
    assert _parse_sse_line(line) == ""


# ==================== _build_request_body ====================


def _profile(**kw) -> TranslationProfile:
    defaults = dict(name="t", base_url="http://x/v1", model="m", api_key=None)
    defaults.update(kw)
    return TranslationProfile(**defaults)


def test_build_request_body_structure() -> None:
    body = _build_request_body("你好", _profile(temperature=0.7))
    assert body["stream"] is True
    assert body["model"] == "m"
    assert body["temperature"] == 0.7
    # chat_template_kwargs.enable_thinking is False
    ctk = body["chat_template_kwargs"]
    assert ctk["enable_thinking"] is False
    # stream_options 存在
    assert "stream_options" in body
    # messages 含 system + user
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert body["messages"][-1]["content"] == "你好"
    # system 提示词非空
    assert body["messages"][0]["content"]


# ==================== OpenAICompatibleBackend via MockTransport ====================


def _sse_lines(chunks: list[str]) -> bytes:
    """把若干 content 拼成合法 SSE 字节流，末尾 [DONE]。"""
    parts = []
    for c in chunks:
        payload = {"choices": [{"delta": {"content": c}}]}
        parts.append(f"data: {json.dumps(payload)}")
    parts.append("data: [DONE]")
    return ("\n\n".join(parts) + "\n\n").encode("utf-8")


def test_translate_streams_and_concatenates(monkeypatch) -> None:
    """多 chunk SSE 流 -> translate() 拼接完整结果。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="http://test.local/v1")

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=_sse_lines(["Hello", " ", "World"]))
    )

    # 用 transport 替换 httpx.Client 默认 transport
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("dictashift.translation.openai_compatible.httpx.Client", _client_factory)

    result = backend.translate("你好", profile)
    assert result == "Hello World"


def test_translate_http_500_raises_with_status(monkeypatch) -> None:
    """HTTP 500 -> TranslationError(status_code=500)。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="http://test.local/v1")

    transport = httpx.MockTransport(lambda req: httpx.Response(500, content=b"server error"))
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    with pytest.raises(TranslationError) as ei:
        backend.translate("你好", profile)
    assert ei.value.status_code == 500


def test_translate_connect_error_raises_none_status(monkeypatch) -> None:
    """ConnectError -> TranslationError(status_code=None)。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="http://nonexistent.invalid/v1")

    def boom(req):
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(boom)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    with pytest.raises(TranslationError) as ei:
        backend.translate("你好", profile)
    assert ei.value.status_code is None


def test_translate_no_api_key_no_authorization_header(monkeypatch) -> None:
    """无 api_key 时请求无 Authorization 头。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(api_key=None)

    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, content=_sse_lines(["ok"]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    backend.translate("你好", profile)
    assert "Authorization" not in captured or captured["auth"] is None


def test_translate_with_api_key_has_authorization_header(monkeypatch) -> None:
    """有 api_key 时请求带 Authorization 头（不断言具体值，仅查前缀）。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(api_key="sk-test-secret-DO-NOT-LOG")

    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, content=_sse_lines(["ok"]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    backend.translate("你好", profile)
    assert captured["auth"] is not None
    assert captured["auth"].startswith("Bearer ")


# ==================== llama_server 常量 ====================


def test_default_llama_port() -> None:
    assert DEFAULT_LLAMA_PORT == 19120


def test_default_local_profile_base_url() -> None:
    p = LlamaServerBackend.default_local_profile()
    assert p.base_url == "http://127.0.0.1:19120/v1"
    assert p.api_key is None


def test_default_local_profile_custom_port() -> None:
    p = LlamaServerBackend.default_local_profile(port=12345)
    assert p.base_url == "http://127.0.0.1:12345/v1"


# ==================== 内置免登录翻译项 ====================


def test_builtin_free_profiles_backend_types() -> None:
    """builtin_free_profiles 返回 (Google, Bing)，backend_type 正确。"""
    google, bing = builtin_free_profiles()
    assert google is BUILTIN_GOOGLE
    assert bing is BUILTIN_BING
    assert BUILTIN_GOOGLE.backend_type == "google"
    assert BUILTIN_BING.backend_type == "bing"


def test_builtin_profile_names_unique() -> None:
    """内置项 name 唯一。"""
    names = builtin_profile_names()
    assert len(names) == 2
    assert len(names) == len(set(names))
    assert BUILTIN_GOOGLE.name in names
    assert BUILTIN_BING.name in names


def test_builtin_profiles_empty_url_model() -> None:
    """内置项 base_url/model 为空（免登录服务无需配置）。"""
    for p in builtin_free_profiles():
        assert p.base_url == ""
        assert p.model == ""
        assert p.api_key is None


def test_builtin_name_for_backend_type_mapping() -> None:
    """backend_type -> 内置项 name 映射正确；openai_compatible 返回 None。"""
    assert builtin_name_for_backend_type("google") == BUILTIN_GOOGLE.name
    assert builtin_name_for_backend_type("bing") == BUILTIN_BING.name
    assert builtin_name_for_backend_type("openai_compatible") is None


def test_builtin_profile_by_name() -> None:
    """按 name 查找内置项；未命中返回 None。"""
    assert builtin_profile_by_name(BUILTIN_GOOGLE.name) is BUILTIN_GOOGLE
    assert builtin_profile_by_name(BUILTIN_BING.name) is BUILTIN_BING
    assert builtin_profile_by_name("不存在") is None

