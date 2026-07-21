"""音频输入设备枚举 (供设置窗口麦克风下拉用)。

保持简单：用 sounddevice.query_devices 列出所有输入设备，返回
[{name, index, default_samplerate}, ...]。
"""

from __future__ import annotations

import logging

import sounddevice as sd

logger = logging.getLogger(__name__)


def list_input_devices() -> list[dict]:
    """列出系统可用的输入设备。

    返回 list[dict]，每个元素包含：
    - name: 设备名 (str)
    - index: sounddevice 设备索引 (int)
    - default_samplerate: 默认采样率 (float)

    实现说明：sounddevice.query_devices() 返回的设备 dict 含 'max_input_channels'
    字段，>0 即表示可作为输入设备。此处不抛异常 (设备枚举失败时返回空列表，
    由调用方决定如何提示用户)。
    """
    result: list[dict] = []
    try:
        all_devices = sd.query_devices()
    except Exception:  # noqa: BLE001 - 设备枚举失败不应使设置窗口崩溃
        logger.exception("[devices] 查询音频设备失败")
        return result

    for index, info in enumerate(all_devices):
        try:
            max_in = int(info.get("max_input_channels", 0))
        except (TypeError, ValueError):
            max_in = 0
        if max_in <= 0:
            continue
        result.append(
            {
                "name": str(info.get("name", f"device#{index}")),
                "index": index,
                "default_samplerate": float(info.get("default_samplerate", 0.0)),
            }
        )
    return result
