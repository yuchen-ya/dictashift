"""免费翻译服务限流自动切换 + 后端路由分发。

FreeFailoverBackend：profile.backend_type 指定的免费服务优先，
限流/服务故障时自动切换到另一个免费服务重试一次。LLM（openai_compatible）
路径不经由此后端，不受影响。

DispatcherBackend：按 profile.backend_type 路由——openai_compatible 走
OpenAICompatibleBackend，google/bing 走 FreeFailoverBackend。controller
只持有此后端即可。

不变量 (AGENTS.md 5.4, 12)：
- LLM 路径不得受免费后端影响。
- 切换日志不含敏感信息（无 api_key/JWT/录音内容）。
"""

import logging
from typing import Iterator

from .base import TranslationBackend, TranslationProfile, TranslationError
from .bing_edge import BingEdgeBackend
from .google_free import GoogleFreeBackend
from .openai_compatible import OpenAICompatibleBackend

logger = logging.getLogger(__name__)

# 限流/服务故障状态码：命中即触发 failover 切换。
# 401 仅在 bing 内部刷新重试后仍失败时到达这里（见 bing_edge.py）。
RATE_LIMIT_STATUS = {401, 403, 429}


def _is_failover_signal(err: TranslationError) -> bool:
    """判断异常是否为切换信号。

    切换条件：status_code in {401, 403, 429} 或 status_code >= 500。
    网络错误（status_code=None）不切换——本地断网时切换无意义。
    """
    code = err.status_code
    if code is None:
        return False
    if code in RATE_LIMIT_STATUS:
        return True
    return code >= 500


class FreeFailoverBackend(TranslationBackend):
    """免费服务限流自动切换。

    primary 由 profile.backend_type 决定（"google"/"bing"），另一个兜底。
    失败信号为限流/服务故障时自动切换到兜底重试一次；网络错误不切换直接抛。
    LLM 配置不经由此后端，不受影响。
    """

    def __init__(self) -> None:
        self._google = GoogleFreeBackend()
        self._bing = BingEdgeBackend()

    def translate_stream(self, text: str, profile: TranslationProfile) -> Iterator[str]:
        """按 backend_type 选 primary，失败则切兜底重试一次。

        实现要点：translate_stream 是生成器，异常在迭代时才抛出。为保证
        "先拿到结果再 yield"，先完整消费 primary 的输出（这两个后端本来
        就只 yield 一次），成功则 yield 一次；失败信号则尝试兜底。
        """
        primary, fallback = self._select(profile)

        try:
            # primary 只 yield 一次；join 后拿到完整结果
            primary_result = "".join(primary.translate_stream(text, profile))
        except TranslationError as err:
            if not _is_failover_signal(err):
                # 网络错误等：不切换，直接抛 primary 异常
                raise
            logger.warning(
                f"[Translation/Failover] primary({profile.backend_type}) 失败 "
                f"status={err.status_code}，切换兜底重试"
            )
            # 切换兜底，重试一次
            fallback_result = "".join(fallback.translate_stream(text, profile))
            yield fallback_result
            return

        yield primary_result

    def _select(
        self, profile: TranslationProfile
    ) -> tuple[TranslationBackend, TranslationBackend]:
        """按 profile.backend_type 返回 (primary, fallback)。"""
        if profile.backend_type == "bing":
            return self._bing, self._google
        # google 或未知值默认以 google 为 primary
        return self._google, self._bing


class DispatcherBackend(TranslationBackend):
    """按 profile.backend_type 路由的统一后端。

    - openai_compatible -> OpenAICompatibleBackend（LLM 路径，不受 failover 影响）。
    - google / bing -> FreeFailoverBackend（免费服务 + 限流自动切换）。

    controller 只需持有此后端即可。
    """

    def __init__(self) -> None:
        self._llm = OpenAICompatibleBackend()
        self._free = FreeFailoverBackend()

    def translate_stream(self, text: str, profile: TranslationProfile) -> Iterator[str]:
        """按 backend_type 路由到对应后端。

        openai_compatible 走 LLM 路径（透传，不经过 failover）；
        google/bing 走免费 failover 路径。
        """
        if profile.backend_type == "openai_compatible":
            yield from self._llm.translate_stream(text, profile)
            return
        yield from self._free.translate_stream(text, profile)
