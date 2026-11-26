"""平台设置。"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_WEB_PORT = 5173
"""Web 开发服务器（vite）的端口。它把 /api 代理到后端，浏览器发来的 Host、Origin 是它的端口。"""


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
    web_port: int = DEFAULT_WEB_PORT
    """Web 页面所在的端口。API 只接受 Host 和 Origin 为本机这个端口或 API 端口的请求。"""

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

    @property
    def plugin_config_path(self) -> Path:
        """插件的用户配置：用户确认过的连接地址（endpoints）及审计记录。不进任何仓库。"""
        return self.home / "plugin-config.db"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """读取 DUOHELM_HOME（默认 ~/.duohelm）、DUOHELM_DEV、DUOHELM_TASK_WORKERS、
        DUOHELM_WEB_PORT（默认 5173）。"""
        env = os.environ if env is None else env
        raw = env.get("DUOHELM_HOME", "").strip()
        home = Path(raw).expanduser() if raw else Path.home() / ".duohelm"
        dev = env.get("DUOHELM_DEV", "").strip() == "1"
        workers_raw = env.get("DUOHELM_TASK_WORKERS", "").strip()
        task_workers = int(workers_raw) if workers_raw else 1
        if task_workers < 1:
            raise ValueError(f"DUOHELM_TASK_WORKERS 必须是正整数，当前为 {workers_raw}")
        web_raw = env.get("DUOHELM_WEB_PORT", "").strip()
        web_port = int(web_raw) if web_raw else DEFAULT_WEB_PORT
        if not 1 <= web_port <= 65535:
            raise ValueError(f"DUOHELM_WEB_PORT 必须是 1–65535 的整数，当前为 {web_raw}")
        return cls(home=home, dev=dev, task_workers=task_workers, web_port=web_port)


def is_loopback(host: str) -> bool:
    """host 是否只在本机可访问。现阶段没有登录，监听非本机地址会把 API 暴露给局域网。"""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
