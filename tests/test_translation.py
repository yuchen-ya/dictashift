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
    SYSTEM_PROMPT_ZH2EN,
    TranslationError,
    TranslationProfile,
    build_no_think_candidates,
    builtin_free_profiles,
    builtin_name_for_backend_type,
    builtin_profile_by_name,
    builtin_profile_names,
    fetch_available_models,
)
from dictashift.translation.openai_compatible import (
    _ThinkTagFilter,
    _build_request_body,
    _parse_sse_line,
)


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


def test_parse_sse_ignores_reasoning_fields() -> None:
    """reasoning_content / reasoning 字段应被忽略，不混入译文。"""
    line1 = 'data: {"choices":[{"delta":{"reasoning_content":"思考过程"}}]}'
    line2 = 'data: {"choices":[{"delta":{"reasoning":"internal thought"}}]}'
    line3 = 'data: {"choices":[{"delta":{"reasoning_content":"step","content":"Hello"}}]}'
    assert _parse_sse_line(line1) is None
    assert _parse_sse_line(line2) is None
    assert _parse_sse_line(line3) == "Hello"


# ==================== _build_request_body ====================


def _profile(**kw) -> TranslationProfile:
    defaults = dict(name="t", base_url="http://x/v1", model="m", api_key=None)
    defaults.update(kw)
    return TranslationProfile(**defaults)


def test_build_request_body_structure_omits_temperature() -> None:
    """默认请求体不含 temperature（即使旧配置 profile 带有数值），且按需合并关闭思考参数。"""
    body = _build_request_body("你好", _profile(temperature=0.7))
    assert body["stream"] is True
    assert body["model"] == "m"
    assert "temperature" not in body
    assert "stream_options" in body
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert body["messages"][-1]["content"] == "你好"
    assert body["messages"][0]["content"]

    body_with_extra = _build_request_body(
        "你好", _profile(), no_think_extra={"reasoning_effort": "none"}
    )
    assert body_with_extra["reasoning_effort"] == "none"
    assert "temperature" not in body_with_extra


# ==================== build_no_think_candidates ====================


def test_no_think_candidates_zhipu_doubao_claude() -> None:
    for url, model in (
        ("https://open.bigmodel.cn/api/paas/v4", "glm-4.5"),
        ("https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-1-6"),
        ("https://api.anthropic.com/v1", "claude-sonnet-4"),
    ):
        cands = build_no_think_candidates(url, model)
        assert cands[0] == {"thinking": {"type": "disabled"}}
        assert cands[1] == {"reasoning_effort": "none"}
        assert cands[2] == {"enable_thinking": False}
        assert cands[-1] == {}


def test_no_think_candidates_qwen_deepseek_cloud_vs_local() -> None:
    cloud = build_no_think_candidates("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-8b")
    assert cloud == [
        {"enable_thinking": False},
        {"chat_template_kwargs": {"enable_thinking": False}},
        {"thinking": {"type": "disabled"}},
        {"reasoning_effort": "none"},
        {},
    ]
    sf = build_no_think_candidates("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-R1")
    assert sf[0] == {"enable_thinking": False}
    assert sf[-1] == {}

    local = build_no_think_candidates("http://127.0.0.1:19120/v1", "qwen3-4b")
    assert local == [
        {"chat_template_kwargs": {"enable_thinking": False}},
        {"enable_thinking": False},
        {"reasoning_effort": "none"},
        {"thinking": {"type": "disabled"}},
        {},
    ]


def test_no_think_candidates_openai_gemini_openrouter() -> None:
    for url, model in (
        ("https://api.openai.com/v1", "gpt-5"),
        ("https://api.openai.com/v1", "o3-mini"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
        ("https://openrouter.ai/api/v1", "custom/model"),
    ):
        cands = build_no_think_candidates(url, model)
        assert cands == [{"reasoning_effort": "none"}, {}]


def test_no_think_candidates_local_generic_and_unknown_cloud() -> None:
    local_proxy = build_no_think_candidates("http://localhost:8317/v1", "foo1-model")
    assert local_proxy == [
        {"reasoning_effort": "none"},
        {"chat_template_kwargs": {"enable_thinking": False}},
        {"enable_thinking": False},
        {"thinking": {"type": "disabled"}},
        {},
    ]
    unknown_cloud = build_no_think_candidates("https://api.custom-gateway.example/v1", "my-model")
    assert unknown_cloud == [
        {"reasoning_effort": "none"},
        {"enable_thinking": False},
        {"thinking": {"type": "disabled"}},
        {"chat_template_kwargs": {"enable_thinking": False}},
        {},
    ]


# ==================== _ThinkTagFilter ====================


def test_think_tag_filter_basic_and_cross_chunk() -> None:
    f = _ThinkTagFilter()
    chunks = ["<th", "ink>secret reasoning", "</th", "ink>", "\n\n", "Hello world"]
    out = "".join(f.feed(c) for c in chunks) + f.flush()
    assert out == "Hello world"


def test_think_tag_filter_thought_and_case_insensitive() -> None:
    f = _ThinkTagFilter()
    out = f.feed("<THINK>abc</THINK>\n  ") + f.feed("<Thought>def</Thought>\r\nResult") + f.flush()
    assert out == "Result"


def test_think_tag_filter_preserves_normal_lt_and_partial_tail() -> None:
    f = _ThinkTagFilter()
    out = f.feed("a < b and c < 5 and <thinker>ok</thinker>") + f.flush()
    assert out == "a < b and c < 5 and <thinker>ok</thinker>"

    f2 = _ThinkTagFilter()
    out2 = f2.feed("prefix <th") + f2.flush()
    assert out2 == "prefix <th"


def test_think_tag_filter_unclosed_think_not_leaked() -> None:
    f = _ThinkTagFilter()
    out = f.feed("Hi <think>secret unclosed") + f.flush()
    assert out == "Hi "


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


def test_build_request_body_custom_system_prompt() -> None:
    """自定义 system prompt 覆盖默认翻译提示词（向后兼容：缺省仍用 ZH2EN）。"""
    body = _build_request_body("你好", _profile(), system_prompt="自定义提示词")
    assert body["messages"][0]["content"] == "自定义提示词"
    # 缺省仍为默认翻译提示词
    body_default = _build_request_body("你好", _profile())
    assert body_default["messages"][0]["content"] == SYSTEM_PROMPT_ZH2EN


def test_rewrite_stream_uses_custom_prompt(monkeypatch) -> None:
    """rewrite_stream：请求体使用自定义 system prompt，流式聚合正常。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="http://test.local/v1")

    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, content=_sse_lines(["毒舌", "版"]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    result = "".join(backend.rewrite_stream("原文", profile, "自定义提示词"))
    assert result == "毒舌版"
    assert captured["body"]["messages"][0]["content"] == "自定义提示词"
    assert captured["body"]["messages"][-1]["content"] == "原文"


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


# ==================== 400/422 降级 + 缓存 + 非 400/422 不重试 ====================


def test_translate_400_fallback_and_cache_hit(monkeypatch) -> None:
    """首策略 400 静默降级至下一策略，命中后写入缓存，第二次调用一次命中。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="https://api.custom.dev/v1", model="my-model")
    seen_bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        seen_bodies.append(body)
        # 拒绝 reasoning_effort，接受 enable_thinking=False
        if "reasoning_effort" in body:
            return httpx.Response(400, content=b'{"error":"unknown param reasoning_effort"}')
        return httpx.Response(200, content=_sse_lines(["Hello"]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    assert backend.translate("你好", profile) == "Hello"
    assert len(seen_bodies) == 2
    assert "reasoning_effort" in seen_bodies[0]
    assert seen_bodies[1].get("enable_thinking") is False
    assert all("temperature" not in b for b in seen_bodies)

    # 第二次直接命中缓存（仅发 1 次请求）
    assert backend.translate("你好", profile) == "Hello"
    assert len(seen_bodies) == 3
    assert seen_bodies[2].get("enable_thinking") is False


def test_translate_422_fallback_to_bare_request_and_strip_think_tags(monkeypatch) -> None:
    """所有关闭思考参数均返回 422 时降级为裸请求 {}，并流式剥离 <think> 与 reasoning_content。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="https://api.openai.com/v1", model="o3-mini")
    seen_bodies: list[dict] = []

    sse_payload = (
        'data: {"choices":[{"delta":{"reasoning_content":"secret"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"<th"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"ink>hidden</think>\\n\\nHi"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" there"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode("utf-8")

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        seen_bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(422, content=b"unsupported parameter")
        return httpx.Response(200, content=sse_payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    assert backend.translate("你好", profile) == "Hi there"
    assert len(seen_bodies) == 2
    assert "reasoning_effort" not in seen_bodies[1]
    assert "temperature" not in seen_bodies[1]

    # 二次调用直接走裸请求缓存
    assert backend.translate("你好", profile) == "Hi there"
    assert len(seen_bodies) == 3
    assert "reasoning_effort" not in seen_bodies[2]


def test_translate_stale_cache_invalidated_on_400(monkeypatch) -> None:
    """缓存策略后续若因服务端变更返回 400，应清除缓存并降级到后续候选。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="https://api.openai.com/v1", model="gpt-5")
    reject_reasoning = False
    seen_bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        seen_bodies.append(body)
        if reject_reasoning and "reasoning_effort" in body:
            return httpx.Response(400, content=b"no longer supported")
        return httpx.Response(200, content=_sse_lines(["ok"]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    assert backend.translate("你好", profile) == "ok"
    reject_reasoning = True
    assert backend.translate("你好", profile) == "ok"
    assert "reasoning_effort" not in seen_bodies[-1]


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_translate_non_400_422_does_not_retry(monkeypatch, status_code: int) -> None:
    """401 / 429 / 500 必须立即失败，绝不重试其他候选策略。"""
    backend = OpenAICompatibleBackend()
    profile = _profile(base_url="https://api.custom.dev/v1", model="my-model")
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, content=b"secret-body-must-not-leak")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        "dictashift.translation.openai_compatible.httpx.Client",
        lambda *a, **k: real_client(*a, transport=transport, **k),
    )

    with pytest.raises(TranslationError) as ei:
        backend.translate("你好", profile)
    assert ei.value.status_code == status_code
    assert calls == 1
    assert "secret-body-must-not-leak" not in str(ei.value)


# ==================== fetch_available_models ====================


def test_fetch_available_models_openai_data_and_endpoint_normalization() -> None:
    """正常解析 data 列表，自动剥离 /chat/completions，正确处理 Authorization 与去重。"""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["auth"] = req.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o-mini"},
                    {"id": "gpt-5"},
                    {"id": "gpt-4o-mini"},
                    {"id": "  "},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        models = fetch_available_models(
            "https://api.openai.com/v1/chat/completions/",
            api_key="sk-secret-value",
            client=client,
        )

    assert captured["url"] == "https://api.openai.com/v1/models"
    assert captured["auth"] == "Bearer sk-secret-value"
    assert models == ["gpt-4o-mini", "gpt-5"]


def test_fetch_available_models_supports_models_key_and_list() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"models": [{"name": "qwen3-8b"}, {"id": "deepseek-v3"}, "llama3"]}
            )
        )
    ) as client:
        assert fetch_available_models("http://127.0.0.1:19120/v1", client=client) == [
            "qwen3-8b",
            "deepseek-v3",
            "llama3",
        ]

    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=["m1", "m2"]))
    ) as client:
        assert fetch_available_models("http://127.0.0.1:19120/v1", client=client) == ["m1", "m2"]


def test_fetch_available_models_errors_and_no_secret_leak() -> None:
    with pytest.raises(TranslationError):
        fetch_available_models("   ")

    secret = "sk-do-not-leak-12345"
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(401, content=b"invalid key"))
    ) as client:
        with pytest.raises(TranslationError) as ei:
            fetch_available_models("https://api.openai.com/v1", api_key=secret, client=client)
        assert ei.value.status_code == 401
        assert secret not in str(ei.value)

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(TranslationError) as ei2:
            fetch_available_models("https://api.openai.com/v1", api_key=secret, client=client)
        assert ei2.value.status_code is None
        assert secret not in str(ei2.value)

