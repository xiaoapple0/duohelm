"""任务系统：SQLite 任务表、后台 worker、进度与日志、失败重试。设计见 docs/subsystems/tasks.md。"""

from duohelm.tasks.store import (
    ALLOWED_TRANSITIONS,
    Claim,
    DuplicateTaskError,
    InvalidTransitionError,
    TaskLog,
    TaskNotFoundError,
    TaskRecord,
    TaskStatus,
    TaskStore,
)
from duohelm.tasks.types import (
    RetryableError,
    TaskCancelled,
    TaskContext,
    TaskParams,
    TaskType,
    TaskTypeRegistry,
    UnknownTaskTypeError,
)
from duohelm.tasks.worker import Worker, default_backoff

__all__ = [
    "ALLOWED_TRANSITIONS",
    "Claim",
    "DuplicateTaskError",
    "InvalidTransitionError",
    "RetryableError",
    "TaskCancelled",
    "TaskContext",
    "TaskLog",
    "TaskNotFoundError",
    "TaskParams",
    "TaskRecord",
    "TaskStatus",
    "TaskStore",
    "TaskType",
    "TaskTypeRegistry",
    "UnknownTaskTypeError",
    "Worker",
    "default_backoff",
]
