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
    dev: bool = False
    """开发模式：注册 demo.sleep 等只用于开发和演示的任务类型。"""
    task_workers: int = 1
    """serve 进程里同时运行的任务数。"""

    @property
    def plugin_dir(self) -> Path:
        return self.home / "plugins"

    @property
    def data_dir(self) -> Path:
        """行情数据：Parquet 文件、清单和数据目录库。"""
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        """元数据库：任务等。"""
        return self.home / "duohelm.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """读取 DUOHELM_HOME（默认 ~/.duohelm）、DUOHELM_DEV、DUOHELM_TASK_WORKERS。"""
        env = os.environ if env is None else env
        raw = env.get("DUOHELM_HOME", "").strip()
        home = Path(raw).expanduser() if raw else Path.home() / ".duohelm"
        dev = env.get("DUOHELM_DEV", "").strip() == "1"
        workers_raw = env.get("DUOHELM_TASK_WORKERS", "").strip()
        task_workers = int(workers_raw) if workers_raw else 1
        if task_workers < 1:
            raise ValueError(f"DUOHELM_TASK_WORKERS 必须是正整数，当前为 {workers_raw}")
        return cls(home=home, dev=dev, task_workers=task_workers)


def is_loopback(host: str) -> bool:
    """host 是否只在本机可访问。现阶段没有登录，监听非本机地址会把 API 暴露给局域网。"""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
