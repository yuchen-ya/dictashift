"""免费翻译后端 + Failover/Dispatcher 路由 + settings 迁移测试。

测试目标 (AGENTS.md 5.4 / 12 / 13.1)：
- GoogleFreeBackend：单段/多段拼接；HTTP 429 -> TranslationError(429)；
  响应结构非法（data[0] 为 None/非列表）-> TranslationError(None)；
  网络错误 -> TranslationError(None)；请求 URL 参数 client=gtx、dt=t、
  sl=zh-CN、tl=en、q=文本。
- BingEdgeBackend：JWT 缓存（两次翻译只请求一次 auth）；401 刷新 JWT 重试
  成功（auth 请求两次）；第二次 401 -> TranslationError(401)；正常响应解析
  data[0].translations[0].text；日志不含 JWT。
- FreeFailoverBackend：主服务限流/服务故障 -> 切兜底；网络错误不切换；
  双失败抛兜底异常；backend_type 决定主服务（google/bing 两种顺序）。
- DispatcherBackend：openai_compatible 路由 LLM（不经 failover）；
  google/bing 路由 failover；LLM 路径 429 不触发免费服务调用。
- settings 迁移：profile_from_dict 无 backend_type -> "openai_compatible"；
  往返序列化保持。

全部用 httpx.MockTransport 或 monkeypatch，禁止真实网络。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Callable

import httpx
import pytest

from dictashift import main
from dictashift.translation import (
    BingEdgeBackend,
    DispatcherBackend,
    FreeFailoverBackend,
    GoogleFreeBackend,
    TranslationError,
    TranslationProfile,
)
from dictashift.translation import failover as failover_mod
from dictashift.translation.bing_edge import _decode_jwt_exp


# ==================== 通用辅助 ====================


def _profile(backend_type: str = "google", **kw) -> TranslationProfile:
    """构造带 backend_type 的 profile，其他字段用任意占位。"""
    defaults = dict(
        name="t",
        base_url="http://unused/v1",
        model="m",
        api_key=None,
        timeout=5.0,
    )
    defaults.update(kw)
    defaults["backend_type"] = backend_type
    return TranslationProfile(**defaults)


def _patch_client(monkeypatch, module_path: str, transport: httpx.MockTransport) -> None:
    """把 module_path 指向的 httpx.Client 替换为强制注入 transport 的工厂。

    与现有 test_translation.py 模式一致：保留真实 httpx.Client，仅在构造时
    覆盖 transport 参数，使 MockTransport 接管全部网络。
    """
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(module_path, _factory)


def _make_jwt(exp_delta: float = 600.0, payload_extra: dict | None = None) -> str:
    """构造一个可被 _decode_jwt_exp 解析的裸 JWT（不验签）。

    payload 的 exp 设为 now + exp_delta；header/payload 用 base64url 无填充编码，
    signature 段任意。
    """
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = {"iat": int(time.time())}
    payload["exp"] = int(time.time()) + int(exp_delta)
    if payload_extra:
        payload.update(payload_extra)
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    sig = b"sig"
    return f"{header.decode()}.{payload_b64.decode()}.{sig.decode()}"


# ==================== GoogleFreeBackend ====================


def _google_response(segments: list[tuple]) -> bytes:
    """构造 google gtx 真实结构响应：[[[译文, 原文, ...], ...], null, "zh-CN", ...]。

    segments: 每段 (译文, 原文)。
    """
    data = [
        [[seg[0], seg[1], None, None, 10] for seg in segments],
        None,
        "zh-CN",
    ]
    return json.dumps(data).encode("utf-8")


class TestGoogleFreeBackend:
    def test_single_segment_concatenates(self, monkeypatch) -> None:
        """单段响应拼接为完整译文。"""
        backend = GoogleFreeBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(
                200,
                content=_google_response([("Hello World", "你好，世界")]),
            )

        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", httpx.MockTransport(handler))

        result = backend.translate("你好，世界", _profile())
        assert result == "Hello World"

    def test_multi_segment_concatenates(self, monkeypatch) -> None:
        """多段响应按顺序拼接。"""
        backend = GoogleFreeBackend()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                content=_google_response([("Hello ", "你好"), ("World", "世界")]),
            )
        )
        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", transport)

        result = backend.translate("你好世界", _profile())
        assert result == "Hello World"

    def test_http_429_raises_with_status(self, monkeypatch) -> None:
        """HTTP 429 -> TranslationError(status_code=429)。"""
        backend = GoogleFreeBackend()
        transport = httpx.MockTransport(lambda req: httpx.Response(429, content=b"rate limit"))
        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", transport)

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile())
        assert ei.value.status_code == 429

    @pytest.mark.parametrize(
        "data",
        [
            "not-a-list-root",        # 根非列表
            [[123]],                  # data[0] 元素非列表（段非列表）
            [["ok", "原文"], None],   # data[0] 第二个段 None 非法
            [[123, "abc"]],           # data[0] 内段首元素非 str
        ],
    )
    def test_illegal_structure_raises_none_status(self, monkeypatch, data) -> None:
        """响应结构非法（根非列表 / data[0] 元素非列表 / 段首元素非 str 等）
        -> TranslationError(None)。"""
        backend = GoogleFreeBackend()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=json.dumps(data).encode())
        )
        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", transport)

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile())
        assert ei.value.status_code is None

    def test_data0_none_raises_none_status(self, monkeypatch) -> None:
        """data[0] 为 None -> TranslationError(None)（独立、明确的非法结构用例）。"""
        backend = GoogleFreeBackend()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=json.dumps([None, "zh-CN"]).encode())
        )
        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", transport)

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile())
        assert ei.value.status_code is None

    def test_data0_not_list_raises_none_status(self, monkeypatch) -> None:
        """data[0] 非列表 -> TranslationError(None)。"""
        backend = GoogleFreeBackend()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=json.dumps(["not-list", "zh-CN"]).encode())
        )
        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", transport)

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile())
        assert ei.value.status_code is None

    def test_network_error_raises_none_status(self, monkeypatch) -> None:
        """网络错误（ConnectError）-> TranslationError(None)。"""
        backend = GoogleFreeBackend()

        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", httpx.MockTransport(boom))

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile())
        assert ei.value.status_code is None

    def test_request_url_contains_required_params(self, monkeypatch) -> None:
        """请求 URL 参数包含 client=gtx、dt=t、sl=zh-CN、tl=en、q=文本。"""
        backend = GoogleFreeBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, content=_google_response([("ok", "原文")]))

        _patch_client(monkeypatch, "dictashift.translation.google_free.httpx.Client", httpx.MockTransport(handler))

        backend.translate("测试文本", _profile())

        url = captured["url"]
        # 基础断言：URL 指向 gtx 端点
        assert "translate.googleapis.com/translate_a/single" in url
        # 必需查询参数（url 编码后）
        assert "client=gtx" in url
        assert "dt=t" in url
        assert "sl=zh-CN" in url
        assert "tl=en" in url
        # q 参数包含文本（中文会被百分号编码，校验解码后等价）
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        assert query["client"] == ["gtx"]
        assert query["dt"] == ["t"]
        assert query["sl"] == ["zh-CN"]
        assert query["tl"] == ["en"]
        assert query["q"] == ["测试文本"]


# ==================== BingEdgeBackend ====================


def _bing_translate_response(text: str = "Hello") -> bytes:
    """构造 Bing translate 响应：[{"translations":[{"text": ...}]}]。"""
    return json.dumps([{"translations": [{"text": text}]}]).encode("utf-8")


class _CountingTransport:
    """按 URL 分派响应并统计 auth/translate 请求次数的 MockTransport 包装。

    用于 Bing：auth 请求（GET auth URL）与翻译请求（POST translate URL）
    走同一 httpx.Client.transport，需要在 handler 内区分 URL。
    """

    def __init__(
        self,
        auth_handler: Callable[[httpx.Request], httpx.Response],
        translate_handler: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self._auth = auth_handler
        self._tr = translate_handler
        self.auth_calls = 0
        self.translate_calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if "translate/auth" in str(req.url):
            self.auth_calls += 1
            return self._auth(req)
        if "api-edge.cognitive.microsofttranslator.com/translate" in str(req.url):
            self.translate_calls += 1
            return self._tr(req)
        # 未知 URL：500 兜底（不应触发）
        return httpx.Response(500, content=b"unknown url")


class TestBingEdgeBackend:
    def test_jwt_cached_single_auth_for_two_translations(self, monkeypatch) -> None:
        """两次翻译只请求一次 auth（JWT 缓存生效）。"""
        backend = BingEdgeBackend()
        counter = _CountingTransport(
            auth_handler=lambda req: httpx.Response(200, text=_make_jwt(exp_delta=600.0)),
            translate_handler=lambda req: httpx.Response(200, content=_bing_translate_response("Hi")),
        )
        _patch_client(monkeypatch, "dictashift.translation.bing_edge.httpx.Client", httpx.MockTransport(counter))

        r1 = backend.translate("你好", _profile(backend_type="bing"))
        r2 = backend.translate("世界", _profile(backend_type="bing"))
        assert r1 == "Hi"
        assert r2 == "Hi"
        assert counter.translate_calls == 2
        # 关键断言：auth 只请求一次（第二次复用缓存）
        assert counter.auth_calls == 1

    def test_401_refresh_jwt_then_retry_success(self, monkeypatch) -> None:
        """第一次 401 -> 刷新 JWT 重试成功。

        auth 请求两次：
          1) _get_valid_jwt 发现缓存过期，首次 fetch（auth #1）
          2) 401 触发 _force_refresh_jwt（auth #2）
        翻译请求两次（初次 401 + 重试 200）。
        """
        backend = BingEdgeBackend()
        # 预置一个已过期的 JWT 缓存，使 _get_valid_jwt 必须发首次 auth 请求。
        jwt_expired = _make_jwt(exp_delta=-100.0)
        backend._jwt = jwt_expired
        backend._jwt_exp = _decode_jwt_exp(jwt_expired)
        assert backend._jwt_exp is not None

        counter = _CountingTransport(
            auth_handler=lambda req: httpx.Response(200, text=_make_jwt(exp_delta=600.0)),
            translate_handler=lambda req: httpx.Response(401, content=b"unauthorized"),
        )

        # 第一次翻译返回 401，第二次（重试）返回 200
        def tr_handler(req: httpx.Request) -> httpx.Response:
            if counter.translate_calls <= 1:
                return httpx.Response(401, content=b"unauthorized")
            return httpx.Response(200, content=_bing_translate_response("OK"))

        counter._tr = tr_handler
        _patch_client(monkeypatch, "dictashift.translation.bing_edge.httpx.Client", httpx.MockTransport(counter))

        result = backend.translate("你好", _profile(backend_type="bing"))
        assert result == "OK"
        assert counter.auth_calls == 2
        assert counter.translate_calls == 2

    def test_second_401_raises(self, monkeypatch) -> None:
        """第二次仍 401 -> TranslationError(401)。"""
        backend = BingEdgeBackend()
        counter = _CountingTransport(
            auth_handler=lambda req: httpx.Response(200, text=_make_jwt(exp_delta=600.0)),
            translate_handler=lambda req: httpx.Response(401, content=b"unauthorized"),
        )
        _patch_client(monkeypatch, "dictashift.translation.bing_edge.httpx.Client", httpx.MockTransport(counter))

        with pytest.raises(TranslationError) as ei:
            backend.translate("你好", _profile(backend_type="bing"))
        assert ei.value.status_code == 401
        # 两次翻译（初次 + 重试），两次 auth（首次缓存刷新无 + 401 强制刷新）
        assert counter.translate_calls == 2

    def test_normal_response_parsed(self, monkeypatch) -> None:
        """正常响应解析 data[0].translations[0].text。"""
        backend = BingEdgeBackend()
        counter = _CountingTransport(
            auth_handler=lambda req: httpx.Response(200, text=_make_jwt(exp_delta=600.0)),
            translate_handler=lambda req: httpx.Response(200, content=_bing_translate_response("Bing 译文")),
        )
        _patch_client(monkeypatch, "dictashift.translation.bing_edge.httpx.Client", httpx.MockTransport(counter))

        result = backend.translate("你好", _profile(backend_type="bing"))
        assert result == "Bing 译文"

    def test_jwt_not_logged(self, monkeypatch, caplog) -> None:
        """日志记录中不出现 JWT 内容（明显假 token "SECRET_JWT" 检测）。"""
        backend = BingEdgeBackend()
        fake_jwt = _make_jwt(exp_delta=600.0)
        # 用一个明显假的 token 字符串嵌入 JWT 中段，验证它绝不进入日志
        fake_jwt_marked = fake_jwt.replace(
            fake_jwt.split(".")[1],
            base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 600, "mark": "SECRET_JWT"}).encode()).rstrip(b"=").decode(),
        )

        counter = _CountingTransport(
            auth_handler=lambda req: httpx.Response(200, text=fake_jwt_marked),
            translate_handler=lambda req: httpx.Response(200, content=_bing_translate_response("ok")),
        )
        _patch_client(monkeypatch, "dictashift.translation.bing_edge.httpx.Client", httpx.MockTransport(counter))

        with caplog.at_level(logging.DEBUG, logger="dictashift.translation.bing_edge"):
            backend.translate("你好", _profile(backend_type="bing"))

        # JWT 标记字符串不得出现在任何日志记录中
        assert "SECRET_JWT" not in caplog.text
        # 整个 JWT 也不应被打印（保守检查 payload 段也不出现）
        assert fake_jwt_marked.split(".")[1] not in caplog.text


# ==================== FreeFailoverBackend ====================


class TestFreeFailoverBackend:
    def test_google_429_falls_back_to_bing(self, monkeypatch) -> None:
        """主服务 google 429 -> 自动切换 bing 成功，返回 bing 译文。"""
        backend = FreeFailoverBackend()

        # google 后端：429；bing 后端：成功
        google_called = {"n": 0}
        bing_called = {"n": 0}

        def google_translate(self, text, profile):
            google_called["n"] += 1
            raise TranslationError("rate limited", status_code=429)

        def bing_translate(self, text, profile):
            bing_called["n"] += 1
            yield "Bing Result"

        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)
        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)

        result = backend.translate("x", _profile(backend_type="google"))
        assert result == "Bing Result"
        assert google_called["n"] == 1
        assert bing_called["n"] == 1

    def test_bing_403_falls_back_to_google(self, monkeypatch) -> None:
        """主服务 bing 403 -> 切换 google 成功。"""
        backend = FreeFailoverBackend()

        def bing_translate(self, text, profile):
            raise TranslationError("forbidden", status_code=403)
            yield  # 使其成为生成器（ unreachable，保持签名一致）

        def google_translate(self, text, profile):
            yield "Google Result"

        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)
        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)

        result = backend.translate("x", _profile(backend_type="bing"))
        assert result == "Google Result"

    def test_primary_503_falls_back(self, monkeypatch) -> None:
        """主服务 503 -> 切换成功。"""
        backend = FreeFailoverBackend()

        def google_translate(self, text, profile):
            raise TranslationError("server error", status_code=503)
            yield

        def bing_translate(self, text, profile):
            yield "Fallback"

        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)
        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)

        result = backend.translate("x", _profile(backend_type="google"))
        assert result == "Fallback"

    def test_network_error_does_not_failover(self, monkeypatch) -> None:
        """主服务网络错误（status_code=None）-> 不切换，直接抛 primary 异常。"""
        backend = FreeFailoverBackend()
        bing_called = {"n": 0}

        def google_translate(self, text, profile):
            raise TranslationError("network", status_code=None)
            yield

        def bing_translate(self, text, profile):
            bing_called["n"] += 1
            yield "should-not-happen"

        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)
        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile(backend_type="google"))
        assert ei.value.status_code is None
        # 兜底未被调用
        assert bing_called["n"] == 0

    def test_both_fail_raises_fallback_error(self, monkeypatch) -> None:
        """双失败 -> 抛出兜底服务的异常。"""
        backend = FreeFailoverBackend()

        def google_translate(self, text, profile):
            raise TranslationError("google 429", status_code=429)
            yield

        def bing_translate(self, text, profile):
            raise TranslationError("bing 503", status_code=503)
            yield

        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)
        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)

        with pytest.raises(TranslationError) as ei:
            backend.translate("x", _profile(backend_type="google"))
        # 抛出的是兜底（bing）的异常，status_code=503
        assert ei.value.status_code == 503
        assert "bing" in str(ei.value).lower()

    def test_backend_type_bing_uses_bing_as_primary(self, monkeypatch) -> None:
        """profile.backend_type=bing -> 主服务为 bing（先于 google）。"""
        backend = FreeFailoverBackend()
        order: list[str] = []

        def bing_translate(self, text, profile):
            order.append("bing")
            yield "bing-ok"

        def google_translate(self, text, profile):
            order.append("google")
            yield "google-ok"

        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)
        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)

        result = backend.translate("x", _profile(backend_type="bing"))
        assert result == "bing-ok"
        # bing 作为 primary 先被调用，成功则不调 google
        assert order == ["bing"]

    def test_backend_type_google_uses_google_as_primary(self, monkeypatch) -> None:
        """profile.backend_type=google -> 主服务为 google（先于 bing）。"""
        backend = FreeFailoverBackend()
        order: list[str] = []

        def google_translate(self, text, profile):
            order.append("google")
            yield "google-ok"

        def bing_translate(self, text, profile):
            order.append("bing")
            yield "bing-ok"

        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)
        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)

        result = backend.translate("x", _profile(backend_type="google"))
        assert result == "google-ok"
        assert order == ["google"]


# ==================== DispatcherBackend ====================


class TestDispatcherBackend:
    def test_openai_compatible_routes_to_llm_not_failover(self, monkeypatch) -> None:
        """openai_compatible profile 路由到 LLM 后端，不经 failover。"""
        dispatcher = DispatcherBackend()

        llm_called = {"n": 0}
        failover_called = {"n": 0}

        from dictashift.translation.openai_compatible import OpenAICompatibleBackend

        def llm_translate(self, text, profile):
            llm_called["n"] += 1
            yield "LLM Result"

        def failover_translate(self, text, profile):
            failover_called["n"] += 1
            yield "should-not-happen"
            raise AssertionError("failover should not be called for LLM profile")

        monkeypatch.setattr(OpenAICompatibleBackend, "translate_stream", llm_translate)
        monkeypatch.setattr(FreeFailoverBackend, "translate_stream", failover_translate)

        result = dispatcher.translate("x", _profile(backend_type="openai_compatible"))
        assert result == "LLM Result"
        assert llm_called["n"] == 1
        assert failover_called["n"] == 0

    def test_google_routes_to_failover(self, monkeypatch) -> None:
        """google profile 路由到 failover。"""
        dispatcher = DispatcherBackend()
        failover_called = {"n": 0}

        def failover_translate(self, text, profile):
            failover_called["n"] += 1
            yield "Failover Result"

        monkeypatch.setattr(FreeFailoverBackend, "translate_stream", failover_translate)

        result = dispatcher.translate("x", _profile(backend_type="google"))
        assert result == "Failover Result"
        assert failover_called["n"] == 1

    def test_bing_routes_to_failover(self, monkeypatch) -> None:
        """bing profile 路由到 failover。"""
        dispatcher = DispatcherBackend()
        failover_called = {"n": 0}

        def failover_translate(self, text, profile):
            failover_called["n"] += 1
            yield "Failover Result"

        monkeypatch.setattr(FreeFailoverBackend, "translate_stream", failover_translate)

        result = dispatcher.translate("x", _profile(backend_type="bing"))
        assert result == "Failover Result"
        assert failover_called["n"] == 1

    def test_llm_429_does_not_invoke_free_services(self, monkeypatch) -> None:
        """LLM 路径 429 不触发任何免费服务调用（LLM 不受限流切换影响）。"""
        dispatcher = DispatcherBackend()

        from dictashift.translation.openai_compatible import OpenAICompatibleBackend

        def llm_translate(self, text, profile):
            raise TranslationError("LLM rate limited", status_code=429)
            yield

        google_called = {"n": 0}
        bing_called = {"n": 0}

        # 即便免费后端方法被调用也要计为违规
        def google_translate(self, text, profile):
            google_called["n"] += 1
            yield "x"

        def bing_translate(self, text, profile):
            bing_called["n"] += 1
            yield "x"

        monkeypatch.setattr(OpenAICompatibleBackend, "translate_stream", llm_translate)
        monkeypatch.setattr(GoogleFreeBackend, "translate_stream", google_translate)
        monkeypatch.setattr(BingEdgeBackend, "translate_stream", bing_translate)

        with pytest.raises(TranslationError) as ei:
            dispatcher.translate("x", _profile(backend_type="openai_compatible"))
        assert ei.value.status_code == 429
        # 关键：免费服务完全未被调用
        assert google_called["n"] == 0
        assert bing_called["n"] == 0


# ==================== settings 迁移 ====================


class TestSettingsMigration:
    def test_profile_from_dict_no_backend_type_defaults_to_openai(self) -> None:
        """profile_from_dict 无 backend_type -> "openai_compatible"。"""
        from dictashift.settings.models import profile_from_dict

        d = {"name": "n", "base_url": "http://x/v1", "model": "m"}
        p = profile_from_dict(d)
        assert p.backend_type == "openai_compatible"

    def test_profile_round_trip_preserves_backend_type(self) -> None:
        """往返序列化保持 backend_type（google / bing / openai_compatible 三种）。"""
        from dictashift.settings.models import profile_from_dict, profile_to_dict

        for bt in ("openai_compatible", "google", "bing"):
            original = TranslationProfile(
                name="n",
                base_url="http://x/v1",
                model="m",
                api_key=None,
                backend_type=bt,
            )
            d = profile_to_dict(original)
            assert d["backend_type"] == bt
            restored = profile_from_dict(d)
            assert restored.backend_type == bt
            assert restored == original


@pytest.mark.parametrize("status_code", [200, 418])
def test_application_logs_omit_google_source_and_response_text(
    monkeypatch, caplog, tmp_path, status_code,
):
    source = "private-source-marker"
    response_marker = "private-response-marker"
    client_type = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=[[[response_marker, source]]])
    )

    def client(**kwargs):
        return client_type(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(main, "_default_log_dir", lambda: tmp_path)

    def configure_without_files(handlers, **kwargs):
        for handler in handlers:
            handler.close()

    monkeypatch.setattr(main.logging, "basicConfig", configure_without_files)
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"), caplog.at_level(
        logging.DEBUG, logger="httpcore",
    ):
        main._setup_logging()
        backend = GoogleFreeBackend()
        if status_code == 200:
            assert backend.translate(source, _profile()) == response_marker
        else:
            with pytest.raises(TranslationError, match="HTTP 418"):
                backend.translate(source, _profile())

    assert source not in caplog.text
    assert response_marker not in caplog.text
