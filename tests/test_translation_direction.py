"""翻译方向（direction）支持 + 文本语种检测测试。

测试目标：
- settings 序列化：direction 往返保持（zh2en/en2zh）；缺 direction 的旧 JSON
  反序列化回退 "zh2en"。
- OpenAICompatibleBackend：direction=en2zh -> 请求体 system prompt 为 EN2ZH；
  显式 zh2en/未知值 -> ZH2EN（防御性）；rewrite_stream 不受 direction 影响。
- GoogleFreeBackend：direction 决定 gtx URL 的 sl/tl；未知值按 zh2en。
- BingEdgeBackend：direction 决定 translate URL 的 from/to；未知值按 zh2en。
- langdetect.detect_direction：纯中文/纯英文/中英混合/空串/纯数字标点。

全部用 httpx.MockTransport 或 monkeypatch，禁止真实网络。
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from dictashift.translation import (
    BingEdgeBackend,
    GoogleFreeBackend,
    OpenAICompatibleBackend,
    TranslationProfile,
)
from dictashift.translation.langdetect import detect_direction
from dictashift.translation.profiles import SYSTEM_PROMPT_EN2ZH, SYSTEM_PROMPT_ZH2EN
from dictashift.settings.models import profile_from_dict, profile_to_dict


# ==================== 通用辅助 ====================


def _profile(backend_type: str = "openai_compatible", **kw) -> TranslationProfile:
    """构造带 backend_type 的 profile，其他字段用任意占位。"""
    defaults = dict(
        name="t",
        base_url="http://test.local/v1",
        model="m",
        api_key=None,
        timeout=5.0,
    )
    defaults.update(kw)
    defaults["backend_type"] = backend_type
    return TranslationProfile(**defaults)


def _patch_client(monkeypatch, module_path: str, transport: httpx.MockTransport) -> None:
    """把 module_path 指向的 httpx.Client 替换为强制注入 transport 的工厂。"""
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(module_path, _factory)


def _sse_lines(chunks: list[str]) -> bytes:
    """把若干 content 拼成合法 SSE 字节流，末尾 [DONE]。"""
    parts = []
    for c in chunks:
        payload = {"choices": [{"delta": {"content": c}}]}
        parts.append(f"data: {json.dumps(payload)}")
    parts.append("data: [DONE]")
    return ("\n\n".join(parts) + "\n\n").encode("utf-8")


def _google_response(text: str = "ok") -> bytes:
    """构造 google gtx 真实结构响应。"""
    return json.dumps([[[text, "原文", None, None, 10]], None, "zh-CN"]).encode("utf-8")


def _make_jwt(exp_delta: float = 600.0) -> str:
    """构造一个可被 bing 后端解析 exp 的裸 JWT（不验签）。"""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = {"iat": int(time.time()), "exp": int(time.time()) + int(exp_delta)}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload_b64.decode()}.sig"


# ==================== settings 序列化 ====================


class TestDirectionSerialization:
    def test_direction_round_trip(self) -> None:
        """往返序列化保持 direction（zh2en / en2zh 两种）。"""
        for direction in ("zh2en", "en2zh"):
            original = _profile(direction=direction)
            d = profile_to_dict(original)
            assert d["direction"] == direction
            restored = profile_from_dict(d)
            assert restored.direction == direction
            assert restored == original

    def test_legacy_json_without_direction_defaults_zh2en(self) -> None:
        """缺 direction 的旧 JSON 反序列化回退 "zh2en"。"""
        d = {"name": "n", "base_url": "http://x/v1", "model": "m"}
        p = profile_from_dict(d)
        assert p.direction == "zh2en"

    def test_profile_default_direction_is_zh2en(self) -> None:
        """不显式指定 direction 时 TranslationProfile 默认为 "zh2en"。"""
        assert _profile().direction == "zh2en"


# ==================== OpenAICompatibleBackend：prompt 选择 ====================


class TestOpenAIDirectionPrompt:
    @pytest.mark.parametrize(
        "direction,expected",
        [
            ("zh2en", SYSTEM_PROMPT_ZH2EN),
            ("en2zh", SYSTEM_PROMPT_EN2ZH),
            ("fr2de", SYSTEM_PROMPT_ZH2EN),  # 未知值防御性按 zh2en
        ],
    )
    def test_translate_stream_uses_prompt_by_direction(
        self, monkeypatch, direction, expected
    ) -> None:
        """translate_stream 按 direction 选择 system prompt。"""
        backend = OpenAICompatibleBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, content=_sse_lines(["ok"]))

        _patch_client(
            monkeypatch,
            "dictashift.translation.openai_compatible.httpx.Client",
            httpx.MockTransport(handler),
        )

        result = backend.translate("文本", _profile(direction=direction))
        assert result == "ok"
        assert captured["body"]["messages"][0]["content"] == expected

    def test_default_direction_uses_zh2en_prompt(self, monkeypatch) -> None:
        """不显式指定 direction（默认 zh2en）-> ZH2EN 提示词。"""
        backend = OpenAICompatibleBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, content=_sse_lines(["ok"]))

        _patch_client(
            monkeypatch,
            "dictashift.translation.openai_compatible.httpx.Client",
            httpx.MockTransport(handler),
        )

        backend.translate("文本", _profile())
        assert captured["body"]["messages"][0]["content"] == SYSTEM_PROMPT_ZH2EN

    def test_rewrite_stream_unaffected_by_direction(self, monkeypatch) -> None:
        """rewrite_stream 仍使用调用方自定义 prompt，不受 direction 影响。"""
        backend = OpenAICompatibleBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content.decode("utf-8"))
            return httpx.Response(200, content=_sse_lines(["改写"]))

        _patch_client(
            monkeypatch,
            "dictashift.translation.openai_compatible.httpx.Client",
            httpx.MockTransport(handler),
        )

        result = "".join(
            backend.rewrite_stream("原文", _profile(direction="en2zh"), "自定义提示词")
        )
        assert result == "改写"
        assert captured["body"]["messages"][0]["content"] == "自定义提示词"


# ==================== GoogleFreeBackend：URL source/target ====================


class TestGoogleDirectionParams:
    @pytest.mark.parametrize(
        "direction,source,target",
        [
            ("zh2en", "zh-CN", "en"),
            ("en2zh", "en", "zh-CN"),
            ("fr2de", "zh-CN", "en"),  # 未知值防御性按 zh2en
        ],
    )
    def test_gtx_url_params_by_direction(
        self, monkeypatch, direction, source, target
    ) -> None:
        """gtx 请求 URL 的 sl/tl 按 direction 构造。"""
        backend = GoogleFreeBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, content=_google_response())

        _patch_client(
            monkeypatch,
            "dictashift.translation.google_free.httpx.Client",
            httpx.MockTransport(handler),
        )

        backend.translate("文本", _profile(backend_type="google", direction=direction))

        query = parse_qs(urlsplit(captured["url"]).query)
        assert query["sl"] == [source]
        assert query["tl"] == [target]


# ==================== BingEdgeBackend：URL from/to ====================


class TestBingDirectionParams:
    @pytest.mark.parametrize(
        "direction,from_lang,to_lang",
        [
            ("zh2en", "zh-Hans", "en"),
            ("en2zh", "en", "zh-Hans"),
            ("fr2de", "zh-Hans", "en"),  # 未知值防御性按 zh2en
        ],
    )
    def test_translate_url_params_by_direction(
        self, monkeypatch, direction, from_lang, to_lang
    ) -> None:
        """translate 请求 URL 的 from/to 按 direction 构造。"""
        backend = BingEdgeBackend()
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "translate/auth" in url:
                return httpx.Response(200, text=_make_jwt())
            captured["url"] = url
            return httpx.Response(
                200,
                content=json.dumps([{"translations": [{"text": "ok"}]}]).encode(),
            )

        _patch_client(
            monkeypatch,
            "dictashift.translation.bing_edge.httpx.Client",
            httpx.MockTransport(handler),
        )

        result = backend.translate(
            "文本", _profile(backend_type="bing", direction=direction)
        )
        assert result == "ok"
        query = parse_qs(urlsplit(captured["url"]).query)
        assert query["from"] == [from_lang]
        assert query["to"] == [to_lang]


# ==================== langdetect.detect_direction ====================


class TestDetectDirection:
    def test_pure_chinese_returns_zh2en(self) -> None:
        """纯中文（含标点）-> "zh2en"。"""
        assert detect_direction("你好，世界！") == "zh2en"

    def test_pure_english_returns_en2zh(self) -> None:
        """纯英文 -> "en2zh"。"""
        assert detect_direction("Hello, world!") == "en2zh"

    def test_mixed_chinese_dominant_returns_zh2en(self) -> None:
        """中英混合且中文占多数 -> "zh2en"。"""
        assert detect_direction("今天我要翻译一段 English 文本") == "zh2en"

    def test_empty_string_returns_en2zh(self) -> None:
        """空串 -> "en2zh"（约定）。"""
        assert detect_direction("") == "en2zh"

    def test_digits_and_punctuation_return_en2zh(self) -> None:
        """纯数字标点（无有效字符）-> "en2zh"（约定）。"""
        assert detect_direction("12345 ...?!") == "en2zh"
