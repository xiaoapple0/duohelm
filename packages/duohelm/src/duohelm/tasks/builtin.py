"""平台内置的任务类型。"""

from __future__ import annotations

import time
from collections import Counter

from pydantic import BaseModel, Field

from duohelm.registry import PluginRegistry
from duohelm.settings import Settings
from duohelm.tasks.types import RetryableError, TaskContext, TaskParams, TaskType


class NoParams(TaskParams):
    pass


class PluginScanResult(BaseModel):
    total: int
    by_status: dict[str, int]


def plugins_scan(settings: Settings) -> TaskType:
    def handler(ctx: TaskContext, params: NoParams) -> PluginScanResult:
        ctx.log(f"扫描插件目录 {settings.plugin_dir}")
        entries = PluginRegistry([settings.plugin_dir]).scan()
        counts = Counter(str(e.status) for e in entries)
        for e in entries:
            for problem in e.problems:
                ctx.log(f"{e.id}: {problem}", "warning")
        ctx.log(f"共 {len(entries)} 个插件")
        return PluginScanResult(total=len(entries), by_status=dict(counts))

    return TaskType(
        name="plugins.scan",
        params_model=NoParams,
        handler=handler,
        description="重新扫描插件目录，检查清单、SDK 兼容性和依赖",
    )


class SleepParams(TaskParams):
    seconds: float = Field(default=10.0, gt=0, le=600)
    steps: int = Field(default=10, ge=1, le=1000)
    fail_attempts: int = Field(default=0, ge=0, le=10)
    """前几次执行在开始时抛 RetryableError，用来演示自动重试。"""


class SleepResult(BaseModel):
    slept_seconds: float


def demo_sleep_handler(ctx: TaskContext, params: SleepParams) -> SleepResult:
    if ctx.attempt <= params.fail_attempts:
        raise RetryableError(f"演示：第 {ctx.attempt} 次执行故意失败")
    step = params.seconds / params.steps
    for i in range(params.steps):
        ctx.progress(i / params.steps, f"第 {i + 1}/{params.steps} 步")
        time.sleep(step)
    ctx.progress(1.0, "完成")
    return SleepResult(slept_seconds=params.seconds)


DEMO_SLEEP = TaskType(
    name="demo.sleep",
    params_model=SleepParams,
    handler=demo_sleep_handler,
    description="开发演示：按步骤睡眠并汇报进度，可模拟失败重试。只在 DUOHELM_DEV=1 时可用",
)


def builtin_task_types(settings: Settings) -> list[TaskType]:
    types = [plugins_scan(settings)]
    if settings.dev:
        types.append(DEMO_SLEEP)
    return types
