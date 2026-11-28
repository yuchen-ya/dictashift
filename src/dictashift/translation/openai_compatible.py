"""OpenAI-compatible 流式翻译客户端（基于 httpx 同步 Client + SSE）。

协议同时覆盖本地 llama-server / vLLM / 反代与云端 OpenAI-compatible API：
- 默认不发送 temperature 参数，避免部分推理模型或网关报错。
- 默认关闭思考模式（按厂商/模型智能排序候选参数 + HTTP 400/422 静默降级至裸请求 + 线程安全缓存）。
- 流式兜底剥离 <think>...</think> / <thought>...</thought> 标签，并忽略 reasoning_content。
- 提供 fetch_available_models() 拉取 /models 可用模型列表。

不变量：任何日志/异常信息中不得包含 api_key、Authorization 头或服务端响应体内容。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from urllib.parse import urlparse

import httpx

from .base import TranslationBackend, TranslationError, TranslationProfile
from .profiles import SYSTEM_PROMPT_EN2ZH, SYSTEM_PROMPT_ZH2EN

logger = logging.getLogger(__name__)

# 常见本地回环主机名集合
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})
# 匹配独立词边界的 o1 / o3 / o4 模型名（避免误伤 foo1 / pro3 等普通字符串）
_OPENAI_O_SERIES_RE = re.compile(r"(?:^|[^a-z0-9])o[134](?:$|[^a-z0-9])")


def _is_local_base_url(base_url: str) -> bool:
    """判断 base_url 是否指向本机地址（127.0.0.1 / localhost / 0.0.0.0 / ::1）。"""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return True
    lower = raw.lower()
    return any(h in lower for h in ("127.0.0.1", "localhost", "0.0.0.0"))


def _thinking_disabled() -> dict[str, object]:
    return {"thinking": {"type": "disabled"}}


def _enable_thinking_false() -> dict[str, object]:
    return {"enable_thinking": False}


def _chat_template_kwargs_false() -> dict[str, object]:
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _reasoning_effort_none() -> dict[str, object]:
    return {"reasoning_effort": "none"}


def build_no_think_candidates(base_url: str, model: str) -> list[dict[str, object]]:
    """按端点与模型名特征返回有序的关闭思考候选参数列表（末项必为 {} 裸请求兜底）。"""
    m = (model or "").strip().lower()
    u = (base_url or "").strip().lower()
    is_local = _is_local_base_url(base_url)

    # 1. 智谱 / 火山豆包 / Anthropic 兼容
    if any(k in m for k in ("glm", "doubao", "seed", "claude")) or any(
        k in u for k in ("bigmodel", "volces", "ark.", "anthropic")
    ):
        return [
            _thinking_disabled(),
            _reasoning_effort_none(),
            _enable_thinking_false(),
            {},
        ]

    # 2. DashScope / 硅基流动 / Qwen / DeepSeek
    if any(k in m for k in ("qwen", "deepseek")) or any(
        k in u for k in ("dashscope", "siliconflow")
    ):
        if is_local:
            return [
                _chat_template_kwargs_false(),
                _enable_thinking_false(),
                _reasoning_effort_none(),
                _thinking_disabled(),
                {},
            ]
        return [
            _enable_thinking_false(),
            _chat_template_kwargs_false(),
            _thinking_disabled(),
            _reasoning_effort_none(),
            {},
        ]

    # 3. OpenAI / Gemini / OpenRouter
    if (
        any(k in m for k in ("gemini", "gpt"))
        or bool(_OPENAI_O_SERIES_RE.search(m))
        or any(k in u for k in ("openai.com", "googleapis", "openrouter"))
    ):
        return [
            _reasoning_effort_none(),
            {},
        ]

    # 4. 本机通用兼容端点（反代 NewAPI/CLIProxy、llama-server、vLLM、Ollama 等）
    if is_local:
        return [
            _reasoning_effort_none(),
            _chat_template_kwargs_false(),
            _enable_thinking_false(),
            _thinking_disabled(),
            {},
        ]

    # 5. 其他任意未知云端 OpenAI 兼容接口
    return [
        _reasoning_effort_none(),
        _enable_thinking_false(),
        _thinking_disabled(),
        _chat_template_kwargs_false(),
        {},
    ]


class _ThinkTagFilter:
    """流式思考标签过滤器：剥离 <think>...</think> 与 <thought>...</thought>。

    特点：
    - 大小写不敏感，支持标签被任意切分在不同 SSE chunk 中。
    - 闭合标签后的多余前导空白/换行会被自动去除。
    - 普通 `<` 字符（如 `a < b` 或 `<div`）正常透出。
    - 流结束时调用 flush()：若在思考块外部，输出剩余安全缓冲；若思考块未闭合，丢弃避免泄漏。
    """

    _OPEN_TO_CLOSE: tuple[tuple[str, str], ...] = (
        ("<think>", "</think>"),
        ("<thought>", "</thought>"),
    )

    def __init__(self) -> None:
        self._in_think: bool = False
        self._close_tag: str = ""
        self._strip_leading_ws: bool = False
        self._buf: str = ""

    def feed(self, chunk: str) -> str:
        """喂入一段流式文本，返回当前可安全输出的文本。"""
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []

        while self._buf:
            if self._in_think:
                lower = self._buf.lower()
                idx = lower.find(self._close_tag)
                if idx != -1:
                    self._buf = self._buf[idx + len(self._close_tag):]
                    self._in_think = False
                    self._close_tag = ""
                    self._strip_leading_ws = True
                    continue
                keep = self._longest_suffix_prefix(lower, (self._close_tag,))
                self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                break

            if self._strip_leading_ws:
                stripped = self._buf.lstrip()
                if not stripped:
                    self._buf = ""
                    break
                self._buf = stripped
                self._strip_leading_ws = False

            lt_idx = self._buf.find("<")
            if lt_idx == -1:
                out.append(self._buf)
                self._buf = ""
                break
            if lt_idx > 0:
                out.append(self._buf[:lt_idx])
                self._buf = self._buf[lt_idx:]

            lower = self._buf.lower()
            matched_open = False
            for open_tag, close_tag in self._OPEN_TO_CLOSE:
                if lower.startswith(open_tag):
                    self._buf = self._buf[len(open_tag):]
                    self._in_think = True
                    self._close_tag = close_tag
                    matched_open = True
                    break
            if matched_open:
                continue

            if any(open_tag.startswith(lower) for open_tag, _ in self._OPEN_TO_CLOSE):
                break

            out.append(self._buf[0])
            self._buf = self._buf[1:]

        return "".join(out)

    def flush(self) -> str:
        """流结束时冲刷剩余缓冲区。"""
        if self._in_think:
            self._buf = ""
            return ""
        if self._strip_leading_ws:
            self._buf = self._buf.lstrip()
            self._strip_leading_ws = False
        rem = self._buf
        self._buf = ""
        return rem

    @staticmethod
    def _longest_suffix_prefix(text: str, targets: tuple[str, ...]) -> int:
        max_keep = 0
        for target in targets:
            upper = min(len(text), len(target) - 1)
            for k in range(upper, max_keep, -1):
                if text.endswith(target[:k]):
                    max_keep = k
                    break
        return max_keep


def _parse_sse_line(line: str) -> str | None:
    """解析单行 SSE 文本，返回增量文本 delta 或 None。

    规则：
    - 空行或非 `data: ` 前缀行返回 None（忽略注释/事件名等）。
    - `data: [DONE]` 返回 None（由调用方据此结束）。
    - 仅提取 `choices[0].delta.content`，自动忽略 `reasoning_content` / `reasoning`。
    - 解析失败或 content 为 None 时返回 None；成功返回 content 字符串。

    抽为模块级函数以便单元测试直接喂字符串行验证。
    """
    if not line:
        return None
    stripped = line.strip()
    if not stripped:
        return None
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:"):].lstrip()
    if payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
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


def _build_request_body(
    text: str,
    profile: TranslationProfile,
    system_prompt: str = SYSTEM_PROMPT_ZH2EN,
    no_think_extra: dict[str, object] | None = None,
) -> dict:
    """构造 chat/completions 请求体（默认不传 temperature，按需合并关闭思考参数）。"""
    body: dict[str, object] = {
        "model": profile.model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream_options": {"include_usage": True},
    }
    if no_think_extra:
        body.update(copy.deepcopy(no_think_extra))
    return body


def _join_endpoint(base_url: str) -> str:
    """拼接 base_url 与 /chat/completions，处理尾部斜杠与误填后缀。"""
    url = base_url.strip().rstrip("/")
    if url.lower().endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _join_models_endpoint(base_url: str) -> str:
    """规范化 base_url 并拼接 /models（剥离末尾斜杠与误填的 /chat/completions）。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise TranslationError("请先填写 Base URL", status_code=None)
    lower = url.lower()
    if lower.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")].rstrip("/")
    elif lower.endswith("/models"):
        url = url[: -len("/models")].rstrip("/")
    if not url:
        raise TranslationError("Base URL 格式无效", status_code=None)
    return url + "/models"


def _extract_model_ids(payload: object) -> list[str]:
    """从 /models 响应 JSON 中提取模型 ID 列表（去空、保序去重）。"""
    raw_items: object
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            raw_items = payload["data"]
        elif isinstance(payload.get("models"), list):
            raw_items = payload["models"]
        else:
            raw_items = []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model_id = ""
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            raw_id = item.get("id") or item.get("name") or ""
            if isinstance(raw_id, str):
                model_id = raw_id.strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def fetch_available_models(
    base_url: str,
    api_key: str | None = None,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> list[str]:
    """请求 OpenAI 兼容 `/models` 接口并返回可用模型 ID 列表。

    - 自动剥离末尾 `/` 及误填的 `/chat/completions`。
    - 兼容 `{"data": [{"id": ...}]}`、`{"models": [...]}` 与直接列表响应。
    - 任何日志与异常信息均不泄露 `api_key`。
    """
    endpoint = _join_models_endpoint(base_url)
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    try:
        if client is not None:
            response = client.get(endpoint, headers=headers)
        else:
            with httpx.Client(timeout=timeout) as owned_client:
                response = owned_client.get(endpoint, headers=headers)
    except httpx.TimeoutException:
        logger.warning("[Translation] 获取模型列表超时")
        raise TranslationError("获取模型列表超时", status_code=None) from None
    except httpx.InvalidURL:
        raise TranslationError("Base URL 格式无效", status_code=None) from None
    except httpx.TransportError as e:
        logger.warning("[Translation] 获取模型列表网络错误：%s", type(e).__name__)
        raise TranslationError(
            "获取模型列表网络错误，请检查 Base URL", status_code=None
        ) from None
    except ValueError:
        raise TranslationError("Base URL 格式无效", status_code=None) from None

    if response.status_code >= 400:
        if response.status_code in (401, 403):
            msg = f"鉴权失败（HTTP {response.status_code}），请检查 API Key"
        elif response.status_code == 404:
            msg = "服务不支持模型列表接口（HTTP 404），可手动输入模型 ID"
        else:
            msg = f"获取模型列表失败（HTTP {response.status_code}）"
        raise TranslationError(msg, status_code=response.status_code)

    try:
        payload = response.json()
    except ValueError:
        raise TranslationError(
            "模型列表响应格式异常，请手动输入模型 ID", status_code=response.status_code
        ) from None

    return _extract_model_ids(payload)


def _no_think_cache_key(base_url: str, model: str) -> tuple[str, str]:
    return (base_url.strip().rstrip("/").lower(), model.strip().lower())


class OpenAICompatibleBackend(TranslationBackend):
    """OpenAI-compatible 流式翻译后端。

    每次 translate_stream 调用创建独立 httpx.Client（with 语法确保关闭）。
    内置线程安全的关闭思考参数能力缓存：针对 HTTP 400/422 静默降级至下一候选策略
    直至裸请求 `{}`，命中后缓存供后续请求一次命中；非 400/422 错误立即失败不重试。
    """

    def __init__(self) -> None:
        self._no_think_cache: dict[tuple[str, str], dict[str, object]] = {}
        self._no_think_cache_lock = threading.Lock()

    def translate_stream(self, text: str, profile: TranslationProfile):
        """流式翻译，逐段 yield 增量文本。

        错误语义：
        - HTTP 状态码非 2xx：抛 TranslationError(status_code=...)；错误消息不含
          响应体（防止服务端回显用户文本）。
        - httpx.TransportError / 超时：包装为 TranslationError(status_code=None)。
        - 任何异常信息中不含 api_key 或 Authorization 头内容。
        """
        prompt = (
            SYSTEM_PROMPT_EN2ZH
            if profile.direction == "en2zh"
            else SYSTEM_PROMPT_ZH2EN
        )
        yield from self._stream(text, profile, prompt)

    def rewrite_stream(
        self, text: str, profile: TranslationProfile, system_prompt: str
    ):
        """与 translate_stream 相同的流式请求，但使用自定义 system prompt。

        供个性化语气改写复用（复用翻译活动档案的 base_url/model/api_key）；
        错误语义与 translate_stream 完全一致。
        """
        yield from self._stream(text, profile, system_prompt)

    def _stream(self, text: str, profile: TranslationProfile, system_prompt: str):
        """流式 chat/completions 请求的统一实现（translate/rewrite 共用）。"""
        endpoint = _join_endpoint(profile.base_url)
        headers = self._build_headers(profile)
        cache_key = _no_think_cache_key(profile.base_url, profile.model)

        with self._no_think_cache_lock:
            cached = self._no_think_cache.get(cache_key)
            cached_copy = copy.deepcopy(cached) if cached is not None else None

        full_candidates = build_no_think_candidates(profile.base_url, profile.model)
        if cached_copy is not None:
            candidates = [cached_copy] + [
                c for c in full_candidates if c != cached_copy
            ]
        else:
            candidates = full_candidates

        try:
            with httpx.Client(timeout=profile.timeout) as client:
                for idx, extra in enumerate(candidates):
                    body = _build_request_body(
                        text, profile, system_prompt, no_think_extra=extra
                    )
                    with client.stream(
                        "POST", endpoint, json=body, headers=headers
                    ) as response:
                        if (
                            response.status_code in (400, 422)
                            and extra
                            and idx < len(candidates) - 1
                        ):
                            # 仅当含非空关闭思考参数且服务端返回 400/422 时静默降级
                            if cached_copy is not None and idx == 0:
                                with self._no_think_cache_lock:
                                    if self._no_think_cache.get(cache_key) == cached_copy:
                                        self._no_think_cache.pop(cache_key, None)
                            logger.debug(
                                "[Translation] HTTP %d，降级尝试下一个关闭思考策略",
                                response.status_code,
                            )
                            continue
                        if response.status_code >= 400:
                            raise TranslationError(
                                f"翻译请求失败：HTTP {response.status_code}",
                                status_code=response.status_code,
                            )

                        with self._no_think_cache_lock:
                            self._no_think_cache[cache_key] = copy.deepcopy(extra)

                        think_filter = _ThinkTagFilter()
                        for line in response.iter_lines():
                            if isinstance(line, bytes):
                                line = line.decode("utf-8", errors="replace")
                            if line.strip() == "data: [DONE]":
                                break
                            delta = _parse_sse_line(line)
                            if delta is not None:
                                filtered = think_filter.feed(delta)
                                if filtered:
                                    yield filtered
                        tail = think_filter.flush()
                        if tail:
                            yield tail
                        return
        except httpx.TimeoutException as e:
            logger.warning("[Translation] 请求超时")
            raise TranslationError(
                f"翻译请求超时：{type(e).__name__}",
                status_code=None,
            ) from None
        except httpx.TransportError as e:
            logger.warning("[Translation] 网络错误：%s", type(e).__name__)
            raise TranslationError(
                f"翻译请求网络错误：{type(e).__name__}",
                status_code=None,
            ) from None

    def _build_headers(self, profile: TranslationProfile) -> dict:
        """构造请求头。api_key 为空时不带 Authorization。"""
        headers = {"Content-Type": "application/json"}
        if profile.api_key:
            headers["Authorization"] = f"Bearer {profile.api_key}"
        return headers
