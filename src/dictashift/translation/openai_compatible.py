"""OpenAI-compatible 流式翻译客户端（基于 httpx 同步 Client + SSE）。

协议同时覆盖本地 llama-server 与云端 OpenAI-compatible API。
不变量：任何日志/异常信息中不得包含 api_key 或 Authorization 头内容。
"""

import json
import logging

import httpx

from .base import TranslationBackend, TranslationProfile, TranslationError
from .profiles import SYSTEM_PROMPT_ZH2EN

logger = logging.getLogger(__name__)


def _parse_sse_line(line: str) -> str | None:
    """解析单行 SSE 文本，返回增量文本 delta 或 None。

    规则：
    - 空行或非 `data: ` 前缀行返回 None（忽略注释/事件名等）。
    - `data: [DONE]` 返回 None（由调用方据此结束）。
    - 解析失败或 `choices[0].delta.content` 为 None 时返回 None。
    - 成功返回 content 字符串。

    抽为模块级函数以便单元测试直接喂字符串行验证。
    """
    if not line:
        return None
    # 去除行尾换行符后处理；iter_lines 已不含换行，这里做防御
    stripped = line.strip()
    if not stripped:
        return None
    if not stripped.startswith("data:"):
        return None
    # 去掉 "data:" 前缀与一个可选空格
    payload = stripped[len("data:"):].lstrip()
    if payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        # 非 JSON 行（如心跳/注释）忽略
        return None
    choices = obj.get("choices")
    if not choices:
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if content is None:
        return None
    return content


def _build_request_body(text: str, profile: TranslationProfile) -> dict:
    """构造 chat/completions 请求体。"""
    return {
        "model": profile.model,
        "stream": True,
        "temperature": profile.temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_ZH2EN},
            {"role": "user", "content": text},
        ],
        # 关闭 Qwen3 系思考模式
        "chat_template_kwargs": {"enable_thinking": False},
        "stream_options": {"include_usage": True},
    }


def _join_endpoint(base_url: str) -> str:
    """拼接 base_url 与 /chat/completions，处理尾部斜杠。"""
    return base_url.rstrip("/") + "/chat/completions"


class OpenAICompatibleBackend(TranslationBackend):
    """OpenAI-compatible 流式翻译后端。

    每次 translate_stream 调用创建独立 httpx.Client（with 语法确保关闭），
    不复用全局 client；不做重试逻辑。
    """

    def translate_stream(self, text: str, profile: TranslationProfile):
        """流式翻译，逐段 yield 增量文本。

        错误语义：
        - HTTP 状态码非 2xx：抛 TranslationError(status_code=...)；错误消息不含
          响应体（防止服务端回显用户文本），响应体仅限量读取后写 DEBUG 日志。
        - httpx.TransportError / 超时：包装为 TranslationError(status_code=None)。
        - 任何异常信息中不含 api_key 或 Authorization 头内容。
        """
        endpoint = _join_endpoint(profile.base_url)
        body = _build_request_body(text, profile)
        headers = self._build_headers(profile)

        try:
            with httpx.Client(timeout=profile.timeout) as client:
                with client.stream("POST", endpoint, json=body, headers=headers) as response:
                    if response.status_code >= 400:
                        # 安全（开源审查 H1/M3）：错误消息不回显响应体
                        # （服务端可能回显 messages 中的用户转写文本）；
                        # 响应体仅限量读取后写 DEBUG 日志（默认不输出）。
                        error_body = self._read_error_body(response)
                        logger.debug(
                            "[Translation] HTTP %s 响应体（截断）：%s",
                            response.status_code, error_body,
                        )
                        raise TranslationError(
                            f"翻译请求失败：HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    # 手写 SSE 解析：逐行处理 data: 前缀
                    for line in response.iter_lines():
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        if line.strip() == "data: [DONE]":
                            break
                        delta = _parse_sse_line(line)
                        if delta is not None:
                            yield delta
        except httpx.TimeoutException as e:
            # 超时统一包装；不含敏感信息
            logger.warning("[Translation] 请求超时")
            raise TranslationError(
                f"翻译请求超时：{type(e).__name__}",
                status_code=None,
            ) from e
        except httpx.TransportError as e:
            # 网络错误包装；不含敏感信息
            logger.warning(f"[Translation] 网络错误：{type(e).__name__}")
            raise TranslationError(
                f"翻译请求网络错误：{type(e).__name__}",
                status_code=None,
            ) from e

    def _build_headers(self, profile: TranslationProfile) -> dict:
        """构造请求头。api_key 为空时不带 Authorization。"""
        headers = {"Content-Type": "application/json"}
        if profile.api_key:
            headers["Authorization"] = f"Bearer {profile.api_key}"
        return headers

    def _read_error_body(self, response: httpx.Response) -> str:
        """限量读取错误响应体（最多 1KB），仅用于 DEBUG 日志。

        M3：不用 response.read() 全量读入，防止异常服务端返回超大响应体耗尽内存。
        """
        try:
            chunks = []
            remaining = 1024
            for chunk in response.iter_bytes(chunk_size=1024):
                chunks.append(chunk[:remaining])
                remaining -= len(chunk)
                if remaining <= 0:
                    break
            return b"".join(chunks).decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001 - 读取错误体失败不应掩盖原始 HTTP 错误
            return ""
