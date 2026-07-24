"""Google 免费翻译后端（gtx 端点）。

使用桌面 Chrome UA 访问 translate.googleapis.com 的 gtx 端点，
无需登录、无需 API Key。免费服务无流式接口，translate_stream 退化为
单段 yield（先拿到完整结果再一次性产出）。

不变量 (AGENTS.md 12)：
- 本后端不涉及 api_key/Authorization，错误信息中也不含敏感数据。
- 限流信号：HTTP 429/503。
"""

import logging
from typing import Iterator
from urllib.parse import urlencode

import httpx

from .base import TranslationBackend, TranslationProfile, TranslationError

logger = logging.getLogger(__name__)

# 桌面 Chrome UA：gtx 端点对 UA 敏感，移动/非浏览器 UA 可能被拒。
_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_GOOGLE_GTX_URL = "https://translate.googleapis.com/translate_a/single"


def _build_gtx_url(text: str, source: str = "zh-CN", target: str = "en") -> str:
    """拼接 gtx GET 请求 URL。"""
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }
    return f"{_GOOGLE_GTX_URL}?{urlencode(params)}"


class GoogleFreeBackend(TranslationBackend):
    """Google 免费翻译后端（gtx）。

    每次 translate_stream 调用创建独立 httpx.Client（with 语法确保关闭），
    不复用全局 client；不做重试逻辑（重试由 FreeFailoverBackend 负责）。
    """

    def translate_stream(self, text: str, profile: TranslationProfile) -> Iterator[str]:
        """免费翻译：一次性 yield 完整译文。

        错误语义（与 openai_compatible.py 风格一致）：
        - 响应 JSON 结构不符（非列表、data[0] 为 None 等）：TranslationError(status_code=None)。
        - HTTP 非 200：TranslationError(status_code=实际码)。
        - 网络/超时：TranslationError(status_code=None)。
        """
        url = _build_gtx_url(text)
        headers = {"User-Agent": _DESKTOP_CHROME_UA}

        try:
            with httpx.Client(timeout=profile.timeout) as client:
                response = client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            logger.warning("[Translation/Google] 请求超时")
            raise TranslationError(
                f"Google 翻译请求超时：{type(e).__name__}",
                status_code=None,
            ) from e
        except httpx.TransportError as e:
            logger.warning(f"[Translation/Google] 网络错误：{type(e).__name__}")
            raise TranslationError(
                f"Google 翻译网络错误：{type(e).__name__}",
                status_code=None,
            ) from e

        if response.status_code != 200:
            # 限流/服务故障：抛出实际状态码（429/503 等），供 failover 判定。
            # 安全（开源审查 H1）：错误消息不回显响应体——服务端错误页可能
            # 回显请求参数 q=用户转写文本，避免语音内容进入异常消息/日志。
            logger.debug(
                "[Translation/Google] HTTP %s 响应体（截断）：%s",
                response.status_code, response.text[:200],
            )
            raise TranslationError(
                f"Google 翻译请求失败：HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as e:
            # 响应非 JSON
            raise TranslationError(
                f"Google 翻译响应解析失败（非 JSON）：{type(e).__name__}",
                status_code=None,
            ) from e

        # 期望结构：data[0] 为译文段数组，每段 [译文, 原文, ...]
        if (
            not isinstance(data, list)
            or len(data) < 1
            or not isinstance(data[0], list)
        ):
            raise TranslationError(
                "Google 翻译响应解析失败：data[0] 结构不符",
                status_code=None,
            )

        segments = data[0]
        parts: list[str] = []
        for seg in segments:
            if not isinstance(seg, list) or len(seg) < 1 or not isinstance(seg[0], str):
                raise TranslationError(
                    "Google 翻译响应解析失败：译文段结构不符",
                    status_code=None,
                )
            parts.append(seg[0])

        yield "".join(parts)
