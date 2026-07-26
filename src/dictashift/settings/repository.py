"""配置持久化仓库。

不变量 (AGENTS.md 12)：
- 所有配置读写必须经由本 repository，业务模块不得自行散落读写。
- 原子写：先写 .tmp 再 os.replace，避免崩溃留下半写文件。
- 文件损坏（JSON 解析失败）时备份原文件为 .bak 并返回默认值，不抛异常打断启动。
- 目录不存在则创建。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from .models import AppSettings

logger = logging.getLogger(__name__)


def app_data_dir() -> Path:
    """返回应用用户数据目录（不含子项）。

    优先 %APPDATA%\\DictaShift；无 APPDATA 环境变量时回退到 ~/.dictashift。
    判定逻辑与 asr.providers.user_model_dir() / main._default_log_dir() 一致。
    不保证目录存在。
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "DictaShift"
    return Path.home() / ".dictashift"


def _default_settings_path() -> Path:
    """返回默认配置文件路径：app_data_dir()/settings.json。"""
    return app_data_dir() / "settings.json"


class SettingsRepository:
    """AppSettings 的持久化仓库。

    典型用法：
        repo = SettingsRepository()
        settings = repo.load()
        settings.enabled = False
        repo.save(settings)
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = Path(path) if path is not None else _default_settings_path()

    @property
    def path(self) -> Path:
        return self._path

    # ==================== 读取 ====================

    def load(self) -> AppSettings:
        """加载配置。

        - 文件不存在：返回默认 AppSettings。
        - 文件损坏（JSON 解析失败或结构异常）：备份原文件为 .bak，
          记日志，返回默认 AppSettings。
        - 结构可解析：用 from_dict 还原，缺失键自动回退到默认值。
        """
        if not self._path.exists():
            return AppSettings()

        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("[SettingsRepository] 读取配置文件失败: %s", self._path)
            return AppSettings()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._backup_corrupt()
            return AppSettings()

        if not isinstance(data, dict):
            # 顶层不是 dict（例如 JSON 数组或字面量），视为损坏。
            self._backup_corrupt()
            return AppSettings()

        try:
            return AppSettings.from_dict(data)
        except Exception:  # noqa: BLE001 - from_dict 已对缺失键做兜底，此处兜底防止结构异常
            logger.exception(
                "[SettingsRepository] 配置结构异常，回退默认值: %s", self._path
            )
            self._backup_corrupt()
            return AppSettings()

    def _backup_corrupt(self) -> None:
        """把损坏的配置文件备份为 .bak，便于用户排查。"""
        try:
            bak = self._path.with_suffix(self._path.suffix + ".bak")
            shutil.copy2(self._path, bak)
            logger.warning(
                "[SettingsRepository] 配置文件已损坏，已备份到 %s，回退默认配置",
                bak,
            )
        except OSError:
            logger.exception(
                "[SettingsRepository] 备份损坏配置文件失败: %s", self._path
            )

    # ==================== 写入 ====================

    def save(self, settings: AppSettings) -> None:
        """原子写入配置。

        流程：确保父目录存在 -> 写 .tmp -> os.replace 覆盖目标。
        os.replace 在同一卷上为原子操作（Windows 上也保证）。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = settings.to_dict()
        text = json.dumps(data, ensure_ascii=False, indent=2)

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        # 用 replace 策略：先写临时文件，再原子替换。
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path)
