from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from duohelm.tasks import (
    RetryableError,
    TaskContext,
    TaskParams,
    TaskStore,
    TaskType,
    TaskTypeRegistry,
    Worker,
    default_backoff,
)

LEASE = 30_000


class Params(TaskParams):
    n: int = 0


class Result(BaseModel):
    value: int


def registry(
    handler: Callable[[TaskContext, Any], BaseModel | None], **kw: Any
) -> TaskTypeRegistry:
    return TaskTypeRegistry([TaskType(name="t", params_model=Params, handler=handler, **kw)])


def submit(store: TaskStore, params: str = "{}", max_attempts: int = 3) -> int:
    return store.create("t", params, max_attempts=max_attempts).id


@pytest.fixture
def store(tmp_path: Path, clock) -> TaskStore:
    return TaskStore(tmp_path / "duohelm.db", clock)


def test_backoff_schedule() -> None:
    assert [default_backoff(a) for a in (1, 2, 3, 4, 10)] == [
        10_000,
        60_000,
        300_000,
        300_000,
        300_000,
    ]


def test_success_writes_result_and_progress(store: TaskStore) -> None:
    def handler(ctx: TaskContext, p: Params) -> Result:
        ctx.progress(0.5, "half")
        ctx.log("hello")
        return Result(value=p.n * 2)

    task_id = submit(store, '{"n": 21}')
    assert Worker(store, registry(handler)).run_once()
    task = store.get(task_id)
    assert task.status == "succeeded"
    assert task.result == '{"value":42}'
    assert task.progress == 1.0
    assert "hello" in [log.message for log in store.logs(task_id)]


def test_run_once_without_tasks(store: TaskStore) -> None:
    assert not Worker(store, registry(lambda ctx, p: None)).run_once()


def test_retryable_error_requeues(store: TaskStore, clock) -> None:
    calls: list[int] = []

    def handler(ctx: TaskContext, p: Params) -> None:
        calls.append(ctx.attempt)
        if ctx.attempt == 1:
            raise RetryableError("network")

    task_id = submit(store)
    worker = Worker(store, registry(handler))
    worker.run_once()
    task = store.get(task_id)
    assert task.status == "queued"
    assert task.error is not None
    assert "network" in task.error
    assert not worker.run_once()  # 还在退避中
    clock.advance(default_backoff(1))
    worker.run_once()
    assert store.get(task_id).status == "succeeded"
    assert calls == [1, 2]


def test_other_errors_are_not_retried(store: TaskStore) -> None:
    def handler(ctx: TaskContext, p: Params) -> None:
        raise ValueError("bad input")

    task_id = submit(store, max_attempts=3)
    Worker(store, registry(handler)).run_once()
    task = store.get(task_id)
    assert task.status == "failed"
    assert task.attempt == 1
    assert task.error == "ValueError: bad input"
    assert any("Traceback" in log.message for log in store.logs(task_id))


def test_invalid_stored_params_fail(store: TaskStore) -> None:
    task_id = submit(store, '{"n": "not a number"}')
    Worker(store, registry(lambda ctx, p: None)).run_once()
    assert store.get(task_id).status == "failed"


def test_unknown_type_fails(store: TaskStore) -> None:
    task_id = store.create("missing", "{}", max_attempts=3).id
    Worker(store, registry(lambda ctx, p: None)).run_once()
    task = store.get(task_id)
    assert task.status == "failed"
    assert task.error is not None
    assert "missing" in task.error


def test_cancel_while_running(store: TaskStore) -> None:
    steps: list[int] = []

    def handler(ctx: TaskContext, p: Params) -> None:
        for i in range(5):
            if i == 2:
                store.request_cancel(ctx.task_id)  # 模拟用户在第 3 步时点了取消
            ctx.progress(i / 5)
            steps.append(i)

    task_id = submit(store)
    Worker(store, registry(handler)).run_once()
    assert store.get(task_id).status == "cancelled"
    assert steps == [0, 1]


def test_heartbeat_renews_lease_without_progress(tmp_path: Path) -> None:
    # 用真实时钟：handler 运行时间是租约的 3 倍，且从不汇报进度。
    store = TaskStore(tmp_path / "duohelm.db")
    lease_ms = 600
    running = threading.Event()
    release = threading.Event()

    def handler(ctx: TaskContext, p: Params) -> None:
        running.set()
        release.wait(10)

    task_id = submit(store)
    worker = Worker(store, registry(handler), lease_ms=lease_ms)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert running.wait(5)
    deadline = time.monotonic() + lease_ms * 3 / 1000
    while time.monotonic() < deadline:
        assert store.recover_expired(default_backoff) == []
        time.sleep(0.05)
    release.set()
    thread.join(10)
    assert store.get(task_id).status == "succeeded"


def test_result_discarded_after_takeover(
    store: TaskStore, clock, caplog: pytest.LogCaptureFixture
) -> None:
    first_running = threading.Event()
    release_first = threading.Event()

    def handler(ctx: TaskContext, p: Params) -> Result:
        if ctx.attempt == 1:
            first_running.set()
            release_first.wait(10)
            return Result(value=1)
        return Result(value=2)

    types = registry(handler)
    task_id = submit(store)
    old = Worker(store, types, worker_id="w-old", lease_ms=LEASE)
    new = Worker(store, types, worker_id="w-new", lease_ms=LEASE)

    thread = threading.Thread(target=old.run_once)
    thread.start()
    assert first_running.wait(5)
    # 旧 worker 卡住：时钟越过租约（心跳间隔是 10 秒真实时间，这里不会触发续期）
    clock.advance(LEASE + 1)
    assert not new.run_once()  # 恢复过期任务，进入退避
    assert store.get(task_id).status == "queued"
    clock.advance(default_backoff(1))
    assert new.run_once()  # 接管
    assert store.get(task_id).result == '{"value":2}'

    with caplog.at_level(logging.WARNING, logger="duohelm.tasks.worker"):
        release_first.set()
        thread.join(10)

    task = store.get(task_id)
    assert task.status == "succeeded"
    assert task.result == '{"value":2}'
    assert any("已丢弃" in r.getMessage() for r in caplog.records)
    assert any("已丢弃" in log.message and log.attempt == 1 for log in store.logs(task_id))


def test_run_forever_processes_and_stops(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "duohelm.db")
    task_id = submit(store, '{"n": 1}')
    stop = threading.Event()
    worker = Worker(store, registry(lambda ctx, p: Result(value=p.n)), poll_interval=0.05)
    thread = threading.Thread(target=worker.run_forever, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 10
    while store.get(task_id).status != "succeeded" and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert store.get(task_id).status == "succeeded"
