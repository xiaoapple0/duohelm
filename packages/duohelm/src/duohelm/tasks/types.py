"""任务类型：参数模型、handler、注册表，以及 handler 运行时拿到的上下文。

handler 必须可以安全地重复执行：同一个任务可能因为重试或 worker 中断而被执行多次。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from duohelm.tasks.store import Claim, LogLevel, TaskStore

DEFAULT_MAX_ATTEMPTS = 3


class RetryableError(Exception):
    """暂时性的失败（网络抖动、资源被占用等）。只有这类错误和 worker 中断会自动重试。"""


class TaskCancelled(Exception):  # noqa: N818  语义是"已取消"，不是错误
    """handler 在检查点发现任务已被请求取消。"""


class TaskLost(Exception):  # noqa: N818
    """租约已被接管：这次执行不再拥有该任务，结果必须丢弃。"""


class TaskParams(BaseModel):
    """任务参数模型的基类。不接受未声明的字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


Handler = Callable[["TaskContext", Any], BaseModel | None]


@dataclass(frozen=True)
class TaskType:
    name: str
    params_model: type[TaskParams]
    handler: Handler
    description: str = ""
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


class UnknownTaskTypeError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"未注册的任务类型：{name}")
        self.name = name


class TaskTypeRegistry:
    def __init__(self, types: Iterable[TaskType] = ()) -> None:
        self._types: dict[str, TaskType] = {}
        for t in types:
            self.register(t)

    def register(self, task_type: TaskType) -> None:
        if task_type.name in self._types:
            raise ValueError(f"任务类型重复注册：{task_type.name}")
        if task_type.max_attempts < 1:
            raise ValueError(f"{task_type.name}: max_attempts 至少为 1")
        self._types[task_type.name] = task_type

    def get(self, name: str) -> TaskType:
        try:
            return self._types[name]
        except KeyError:
            raise UnknownTaskTypeError(name) from None

    def all(self) -> list[TaskType]:
        return sorted(self._types.values(), key=lambda t: t.name)

    def validate_params(self, name: str, params: Mapping[str, Any]) -> str:
        """校验参数，返回规范化后的 JSON。

        未知类型抛 UnknownTaskTypeError，参数不合法抛 pydantic.ValidationError。
        """
        model = self.get(name).params_model.model_validate(dict(params))
        return model.model_dump_json()


class TaskContext:
    """handler 通过它汇报进度、写日志、检查取消。"""

    def __init__(self, store: TaskStore, claim: Claim, lost: threading.Event) -> None:
        self._store = store
        self._claim = claim
        self._lost = lost

    @property
    def task_id(self) -> int:
        return self._claim.task_id

    @property
    def attempt(self) -> int:
        """第几次执行，从 1 开始。"""
        return self._claim.attempt

    def log(self, message: str, level: LogLevel = "info") -> None:
        self._store.append_log(self.task_id, self.attempt, level, message)

    def progress(self, fraction: float | None, message: str | None = None) -> None:
        """汇报进度（0..1，未知时传 None），同时是一个取消检查点。"""
        if fraction is not None and not 0.0 <= fraction <= 1.0:
            raise ValueError(f"进度必须在 0 到 1 之间，收到 {fraction}")
        self.check_cancelled()
        if not self._store.update_progress(self._claim, fraction, message):
            self._lost.set()
            raise TaskLost

    def check_cancelled(self) -> None:
        """取消检查点。任务被取消时抛 TaskCancelled，租约被接管时抛 TaskLost。"""
        if self._lost.is_set():
            raise TaskLost
        if self._store.is_cancel_requested(self.task_id):
            raise TaskCancelled


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "RetryableError",
    "TaskCancelled",
    "TaskContext",
    "TaskLost",
    "TaskParams",
    "TaskType",
    "TaskTypeRegistry",
    "UnknownTaskTypeError",
]
