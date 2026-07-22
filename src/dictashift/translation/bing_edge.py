"""Bing 翻译后端（Edge 免登录接口）。

通过 edge.microsoft.com/translate/auth 获取裸 JWT（exp 约 10 分钟），
再调用 api-edge.cognitive.microsofttranslator.com/translate 翻译。
无需登录、无需 API Key。

不变量 (AGENTS.md 12)：
- JWT 视同凭据，日志/异常信息中不得打印 JWT 内容。
- 401 = JWT 过期/无效 → 刷新 JWT 重试一次；再 401 视为服务故障。
- 限流信号：429/5xx。
"""

import base64
import json
import logging
import threading
import time
from typing import Iterator

import httpx

from .base import TranslationBackend, TranslationProfile, TranslationError

logger = logging.getLogger(__name__)

# JWT 过期提前量（秒）：留出网络往返余量。
_JWT_EXPIRY_MARGIN = 60.0

_BING_AUTH_URL = "https://edge.microsoft.com/translate/auth"
_BING_TRANSLATE_URL = (
    "https://api-edge.cognitive.microsofttranslator.com/translate"
    "?api-version=3.0&from=zh-Hans&to=en"
)

_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _decode_jwt_exp(token: str) -> float | None:
    """解析 JWT payload 的 exp（不验签）。

    返回 exp 的 Unix 时间戳；解析失败返回 None。
    JWT 结构：header.payload.signature，三段 base64url（无填充）。
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    # 补齐 base64url 填充
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return float(exp)


class BingEdgeBackend(TranslationBackend):
    """Bing（Edge 接口）免费翻译后端。

    JWT 缓存：解析 payload 的 exp（base64url 解码 JSON，不验签），
    提前 60 秒视为过期；无缓存或过期时先获取新 JWT。
    缓存与刷新由 threading.Lock 保护。

    每次 translate_stream 调用创建独立 httpx.Client；
    401 时强制刷新 JWT 重试一次；第二次 401 → TranslationError(status_code=401)。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jwt: str | None = None
        self._jwt_exp: float | None = None

    # ---------- JWT 缓存 ----------

    def _get_valid_jwt(self, timeout: float) -> str:
        """返回未过期的 JWT，必要时刷新。调用方需已持有 _lock 之外的上下文。

        线程安全：内部加锁。若已有有效 JWT 直接返回，否则发起 auth 请求。
        """
        with self._lock:
            now = time.time()
            if (
                self._jwt is not None
                and self._jwt_exp is not None
                and now < self._jwt_exp - _JWT_EXPIRY_MARGIN
            ):
                return self._jwt

        # 锁外发请求（避免持锁期间网络阻塞其他线程）
        jwt = self._fetch_jwt(timeout)
        with self._lock:
            self._jwt = jwt
            self._jwt_exp = _decode_jwt_exp(jwt)
        return jwt

    def _force_refresh_jwt(self, timeout: float) -> str:
        """强制刷新 JWT（无视缓存）。用于 401 重试。"""
        jwt = self._fetch_jwt(timeout)
        with self._lock:
            self._jwt = jwt
            self._jwt_exp = _decode_jwt_exp(jwt)
        return jwt

    def _fetch_jwt(self, timeout: float) -> str:
        """GET auth 端点，返回裸 JWT 文本。

        不打印 JWT 内容。错误语义与 google 一致：
        - HTTP 非 200：TranslationError(status_code=实际码)。
        - 网络/超时：TranslationError(status_code=None)。
        """
        headers = {"User-Agent": _DESKTOP_CHROME_UA}
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(_BING_AUTH_URL, headers=headers)
        except httpx.TimeoutException as e:
            logger.warning("[Translation/Bing] auth 请求超时")
            raise TranslationError(
                f"Bing auth 请求超时：{type(e).__name__}",
                status_code=None,
            ) from e
        except httpx.TransportError as e:
            logger.warning(f"[Translation/Bing] auth 网络错误：{type(e).__name__}")
            raise TranslationError(
                f"Bing auth 网络错误：{type(e).__name__}",
                status_code=None,
            ) from e

        if response.status_code != 200:
            raise TranslationError(
                f"Bing auth 请求失败：HTTP {response.status_code}",
                status_code=response.status_code,
            )
        token = response.text.strip()
        if not token:
            raise TranslationError(
                "Bing auth 响应为空",
                status_code=None,
            )
        return token

    # ---------- 翻译 ----------

    def translate_stream(self, text: str, profile: TranslationProfile) -> Iterator[str]:
        """免费翻译：一次性 yield 完整译文。

        错误语义：
        - 401：强制刷新 JWT 重试一次；再 401 → TranslationError(status_code=401)。
        - HTTP 其他非 200：TranslationError(status_code=实际码)。
        - 网络/超时：TranslationError(status_code=None)。
        - 响应 JSON 结构不符：TranslationError(status_code=None)。
        """
        result = self._translate_once(text, profile.timeout)
        if result is not None:
            yield result
            return

        # 第一次 401（或其它可重试的 None 信号由 _translate_once 内部不抛而返回 None 表示 401）
        logger.warning("[Translation/Bing] 翻译返回 401，刷新 JWT 重试一次")
        self._force_refresh_jwt(profile.timeout)
        result = self._translate_once(text, profile.timeout)
        if result is not None:
            yield result
            return

        # 第二次仍 401
        raise TranslationError(
            "Bing 翻译请求失败：刷新 JWT 后仍 401",
            status_code=401,
        )

    def _translate_once(self, text: str, timeout: float) -> str | None:
        """执行一次翻译请求。

        返回值：
        - str：译文（成功）。
        - None：HTTP 401，调用方应刷新 JWT 重试。
        - 其余错误直接 raise TranslationError。
        """
        jwt = self._get_valid_jwt(timeout)
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
            "User-Agent": _DESKTOP_CHROME_UA,
        }
        body = [{"Text": text}]

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(_BING_TRANSLATE_URL, headers=headers, json=body)
        except httpx.TimeoutException as e:
            logger.warning("[Translation/Bing] 翻译请求超时")
            raise TranslationError(
                f"Bing 翻译请求超时：{type(e).__name__}",
                status_code=None,
            ) from e
        except httpx.TransportError as e:
            logger.warning(f"[Translation/Bing] 翻译网络错误：{type(e).__name__}")
            raise TranslationError(
                f"Bing 翻译网络错误：{type(e).__name__}",
                status_code=None,
            ) from e

        if response.status_code == 401:
            # 信号给调用方重试，不在此抛出
            return None

        if response.status_code != 200:
            error_body = response.text[:500]
            raise TranslationError(
                f"Bing 翻译请求失败：HTTP {response.status_code}，响应：{error_body}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as e:
            raise TranslationError(
                f"Bing 翻译响应解析失败（非 JSON）：{type(e).__name__}",
                status_code=None,
            ) from e

        # 期望结构：data[0].translations[0].text
        try:
            translated = data[0]["translations"][0]["text"]
        except (IndexError, KeyError, TypeError) as e:
            raise TranslationError(
                "Bing 翻译响应解析失败：结构不符",
                status_code=None,
            ) from e

        if not isinstance(translated, str):
            raise TranslationError(
                "Bing 翻译响应解析失败：译文非字符串",
                status_code=None,
            )
        return translated
