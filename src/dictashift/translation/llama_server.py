"""本地 llama-server 翻译后端占位。

第一版不做进程管理（启动/健康检查/重启后续版本实现），
协议与 OpenAI-compatible 完全一致，因此直接继承。
"""

from .base import TranslationProfile
from .openai_compatible import OpenAICompatibleBackend

# llama-server 默认端口
DEFAULT_LLAMA_PORT = 19120


class LlamaServerBackend(OpenAICompatibleBackend):
    """本地 llama-server 后端占位：协议与 OpenAI-compatible 完全一致。

    第一版不做进程管理（启动/健康检查/重启后续版本实现），
    仅提供 default_local_profile() 便捷构造。
    """

    @staticmethod
    def default_local_profile(
        model: str = "local-model",
        port: int = DEFAULT_LLAMA_PORT,
    ) -> TranslationProfile:
        """构造指向本地 llama-server (127.0.0.1:{port}) 的档案。

        本地服务默认无需 api_key。
        """
        return TranslationProfile(
            name="本地翻译",
            base_url=f"http://127.0.0.1:{port}/v1",
            model=model,
            api_key=None,
        )
