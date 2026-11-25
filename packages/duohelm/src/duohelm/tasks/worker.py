"""后台 worker：领取任务、运行 handler、写回结果。

租约由独立的心跳线程续期（间隔为租约时长的 1/3），不依赖 handler 汇报进度。
写回结果时要求 task id、worker id、attempt 都匹配且仍是 running；写回 0 行说明
租约已被接管，结果丢弃并记警告。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import traceback
import uuid

from pydantic import ValidationError

from duohelm.tasks.store import Claim, TaskStore
from duohelm.tasks.types import (
    RetryableError,
    TaskCancelled,
    TaskContext,
    TaskLost,
    TaskTypeRegistry,
    UnknownTaskTypeError,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_MS = 30_000
RETRY_DELAYS_MS = (10_000, 60_000, 300_000)


def default_backoff(attempt: int) -> int:
    """第 1 次失败后等 10 秒，第 2 次 60 秒，之后都是 300 秒。"""
    index = min(max(attempt, 1), len(RETRY_DELAYS_MS)) - 1
    return RETRY_DELAYS_MS[index]


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Worker:
    def __init__(
        self,
        store: TaskStore,
        types: TaskTypeRegistry,
        *,
        worker_id: str | None = None,
        lease_ms: int = DEFAULT_LEASE_MS,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.types = types
        self.worker_id = worker_id or new_worker_id()
        self.lease_ms = lease_ms
        self.poll_interval = poll_interval

    def run_forever(self, stop: threading.Event) -> None:
        """循环领取任务，直到 stop 被设置。stop 之后会等当前任务结束再返回。"""
        while not stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("worker %s 出错", self.worker_id)
                worked = False
            if not worked:
                stop.wait(self.poll_interval)

    def run_once(self) -> bool:
        """恢复过期任务，再领取并执行一个任务。没有可执行的任务时返回 False。"""
        for task_id in self.store.recover_expired(default_backoff):
            logger.warning("任务 %s 的租约已过期，按 worker 中断处理", task_id)
        claim = self.store.claim(self.worker_id, self.lease_ms)
        if claim is None:
            return False
        self._execute(claim)
        return True

    def _execute(self, claim: Claim) -> None:
        lost = threading.Event()
        done = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(claim, lost, done),
            name=f"task-{claim.task_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._run_handler(claim, lost)
        finally:
            done.set()
            heartbeat.join()

    def _heartbeat(self, claim: Claim, lost: threading.Event, done: threading.Event) -> None:
        interval = self.lease_ms / 3 / 1000
        while not done.wait(interval):
            try:
                if not self.store.renew_lease(claim, self.lease_ms):
                    lost.set()
                    return
            except Exception:
                # 一次续期失败（比如数据库忙）不立刻放弃，下一次心跳再试；
                # 一直失败的话租约会过期，由恢复流程接管。
                logger.exception("任务 %s 续期失败", claim.task_id)

    def _run_handler(self, claim: Claim, lost: threading.Event) -> None:
        store = self.store
        ctx = TaskContext(store, claim, lost)
        try:
            task_type = self.types.get(claim.task.type)
            params = task_type.params_model.model_validate_json(claim.task.params)
            result = task_type.handler(ctx, params)
        except TaskCancelled:
            written = store.finish_cancelled(claim)
        except TaskLost:
            written = False
        except RetryableError as exc:
            written = self._fail(claim, f"{type(exc).__name__}: {exc}", retryable=True)
        except (UnknownTaskTypeError, ValidationError) as exc:
            written = self._fail(claim, str(exc), retryable=False)
        except Exception as exc:
            self._log_traceback(claim)
            written = self._fail(claim, f"{type(exc).__name__}: {exc}", retryable=False)
        else:
            payload = result.model_dump_json() if result is not None else None
            written = store.finish_succeeded(claim, payload)
        if not written:
            self._discarded(claim)

    def _fail(self, claim: Claim, error: str, *, retryable: bool) -> bool:
        return self.store.finish_failed(claim, error, retryable=retryable, backoff=default_backoff)

    def _log_traceback(self, claim: Claim) -> None:
        try:
            self.store.append_log(claim.task_id, claim.attempt, "error", traceback.format_exc())
        except Exception:
            logger.exception("写入任务 %s 的日志失败", claim.task_id)

    def _discarded(self, claim: Claim) -> None:
        message = f"第 {claim.attempt} 次执行的结果已丢弃：租约已被接管"
        logger.warning("任务 %s %s（worker %s）", claim.task_id, message, self.worker_id)
        try:
            self.store.append_log(claim.task_id, claim.attempt, "warning", message)
        except Exception:
            logger.exception("写入任务 %s 的日志失败", claim.task_id)
