"""FastAPI 应用。Web 前端只通过这里访问平台能力。"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from duohelm import __version__
from duohelm.api.plugins import plugin_router
from duohelm.api.security import install_origin_guard
from duohelm.api.tasks import task_router
from duohelm.endpoints import EndpointStore
from duohelm.registry import PluginRegistry
from duohelm.settings import Settings
from duohelm.tasks import TaskStore, TaskTypeRegistry, Worker
from duohelm.tasks.builtin import builtin_task_types
from duohelm_sdk import SDK_VERSION

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_S = 30.0


class HealthResponse(BaseModel):
    status: str
    version: str
    sdk_version: str


def create_app(settings: Settings | None = None, *, run_workers: bool = False) -> FastAPI:
    """run_workers=True 时在应用生命周期内启动后台 worker 线程（duohelm serve 使用）。

    测试保持默认的 False，用 Worker.run_once() 手动推进任务，结果不依赖线程时序。
    """
    settings = settings or Settings.from_env()
    registry = PluginRegistry([settings.plugin_dir])
    store = TaskStore(settings.db_path)
    endpoints = EndpointStore(settings.plugin_config_path)
    task_types = TaskTypeRegistry(builtin_task_types(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        if not run_workers:
            yield
            return
        stop = threading.Event()
        threads = [
            threading.Thread(
                target=Worker(store, task_types).run_forever,
                args=(stop,),
                name=f"task-worker-{i}",
                daemon=True,
            )
            for i in range(settings.task_workers)
        ]
        for t in threads:
            t.start()
        try:
            yield
        finally:
            stop.set()
            for t in threads:
                # 等当前任务结束；超时则留给下次启动时的租约恢复处理。
                t.join(timeout=SHUTDOWN_TIMEOUT_S)
                if t.is_alive():
                    logger.warning("%s 在 %s 秒内没有结束，强制退出", t.name, SHUTDOWN_TIMEOUT_S)

    app = FastAPI(title="DuoHelm", version=__version__, lifespan=lifespan)
    install_origin_guard(app, settings)
    app.include_router(task_router(store, task_types))
    app.include_router(plugin_router(registry, endpoints))

    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, sdk_version=SDK_VERSION)

    app.add_api_route("/api/health", health, methods=["GET"], response_model=HealthResponse)
    return app
