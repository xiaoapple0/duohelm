"""平台设置。"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


@dataclass(frozen=True)
class Settings:
    home: Path
    """数据根目录：行情、因子缓存、元数据、插件。"""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def plugin_dir(self) -> Path:
        return self.home / "plugins"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """读取 DUOHELM_HOME；未设置时使用 ~/.duohelm。"""
        env = os.environ if env is None else env
        raw = env.get("DUOHELM_HOME", "").strip()
        home = Path(raw).expanduser() if raw else Path.home() / ".duohelm"
        return cls(home=home)


def is_loopback(host: str) -> bool:
    """host 是否只在本机可访问。现阶段没有登录，监听非本机地址会把 API 暴露给局域网。"""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
