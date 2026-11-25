from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from duohelm.tasks import (
    ALLOWED_TRANSITIONS,
    Claim,
    DuplicateTaskError,
    InvalidTransitionError,
    TaskNotFoundError,
    TaskStatus,
    TaskStore,
)
from duohelm.tasks.store import check_sqlite_version

LEASE = 30_000


def backoff(attempt: int) -> int:
    return 1_000 * attempt


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "duohelm.db"


@pytest.fixture
def store(db: Path, clock) -> TaskStore:
    return TaskStore(db, clock)


def claim(store: TaskStore, worker_id: str = "w1") -> Claim:
    c = store.claim(worker_id, LEASE)
    assert c is not None
    return c


def make_in_state(store: TaskStore, status: TaskStatus) -> int:
    task = store.create("t", "{}", max_attempts=1)
    if status == "queued":
        return task.id
    if status == "cancelled":
        store.request_cancel(task.id)
        return task.id
    c = claim(store)
    if status == "succeeded":
        assert store.finish_succeeded(c, None)
    elif status == "failed":
        assert store.finish_failed(c, "boom", retryable=False, backoff=backoff)
    assert store.get(task.id).status == status
    return task.id


# ---- 基本读写 ----


def test_create_and_get(store: TaskStore, clock) -> None:
    task = store.create("plugins.scan", '{"a": 1}', max_attempts=3)
    got = store.get(task.id)
    assert got.status == "queued"
    assert got.attempt == 0
    assert got.max_attempts == 3
    assert got.params == '{"a": 1}'
    assert got.created_at.timestamp() * 1000 == clock.now
    assert got.created_at.tzinfo is not None


def test_get_missing(store: TaskStore) -> None:
    with pytest.raises(TaskNotFoundError):
        store.get(42)


def test_list_filters_and_cursor(store: TaskStore) -> None:
    ids = [store.create("a" if i % 2 else "b", "{}", max_attempts=1).id for i in range(5)]
    store.request_cancel(ids[0])
    assert [t.id for t in store.list()] == ids[::-1]
    assert [t.id for t in store.list(type="a")] == [ids[3], ids[1]]
    assert [t.id for t in store.list(status="cancelled")] == [ids[0]]
    assert [t.id for t in store.list(limit=2)] == [ids[4], ids[3]]
    assert [t.id for t in store.list(before_id=ids[3])] == [ids[2], ids[1], ids[0]]


def test_logs_incremental(store: TaskStore) -> None:
    task = store.create("t", "{}", max_attempts=1)
    c = claim(store)
    store.append_log(task.id, c.attempt, "info", "one")
    first = store.logs(task.id)
    store.append_log(task.id, c.attempt, "warning", "two")
    more = store.logs(task.id, after_id=first[-1].id)
    assert [log.message for log in more] == ["two"]
    assert more[0].level == "warning"
    assert more[0].attempt == 1


def test_logs_missing_task(store: TaskStore) -> None:
    with pytest.raises(TaskNotFoundError):
        store.logs(42)


# ---- 领取 ----


def test_claim_marks_running(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=3)
    c = claim(store, "w1")
    assert c.task_id == task.id
    assert c.attempt == 1
    got = store.get(task.id)
    assert got.status == "running"
    assert got.worker_id == "w1"
    assert got.lease_expires_at is not None
    assert got.lease_expires_at.timestamp() * 1000 == clock.now + LEASE


def test_claim_skips_running_and_takes_next(store: TaskStore) -> None:
    first = store.create("t", "{}", max_attempts=1)
    second = store.create("t", "{}", max_attempts=1)
    assert claim(store, "w1").task_id == first.id
    assert claim(store, "w2").task_id == second.id
    assert store.claim("w3", LEASE) is None


def test_claim_empty(store: TaskStore) -> None:
    assert store.claim("w1", LEASE) is None


def test_claim_in_id_order(store: TaskStore) -> None:
    a = store.create("t", "{}", max_attempts=1)
    store.create("t", "{}", max_attempts=1)
    assert claim(store).task_id == a.id


def test_claim_respects_run_after(store: TaskStore, clock) -> None:
    store.create("t", "{}", max_attempts=3)
    c = claim(store)
    assert store.finish_failed(c, "flaky", retryable=True, backoff=backoff)
    assert store.claim("w1", LEASE) is None
    clock.advance(backoff(1))
    assert claim(store).attempt == 2


def test_concurrent_claim_only_one_wins(db: Path, clock) -> None:
    TaskStore(db, clock).create("t", "{}", max_attempts=1)
    stores = [TaskStore(db, clock) for _ in range(8)]
    barrier = threading.Barrier(len(stores))
    results: list[Claim | None] = [None] * len(stores)

    def run(i: int) -> None:
        barrier.wait()
        results[i] = stores[i].claim(f"w{i}", LEASE)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(stores))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1


# ---- 状态流转 ----


def test_succeeded_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS["succeeded"] == frozenset()


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_cancel_allowed_only_when_unfinished(store: TaskStore, status: TaskStatus) -> None:
    task_id = make_in_state(store, status)
    if status in ("queued", "running"):
        store.request_cancel(task_id)
    else:
        with pytest.raises(InvalidTransitionError):
            store.request_cancel(task_id)


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_retry_allowed_only_when_failed_or_cancelled(store: TaskStore, status: TaskStatus) -> None:
    task_id = make_in_state(store, status)
    if status in ("failed", "cancelled"):
        assert store.retry(task_id, extra_attempts=3).status == "queued"
    else:
        with pytest.raises(InvalidTransitionError):
            store.retry(task_id, extra_attempts=3)


def test_cancel_queued_is_immediate(store: TaskStore) -> None:
    task = store.create("t", "{}", max_attempts=1)
    got = store.request_cancel(task.id)
    assert got.status == "cancelled"
    assert got.finished_at is not None
    assert store.claim("w1", LEASE) is None


def test_cancel_running_only_flags(store: TaskStore) -> None:
    task = store.create("t", "{}", max_attempts=1)
    c = claim(store)
    got = store.request_cancel(task.id)
    assert got.status == "running"
    assert got.cancel_requested
    assert store.finish_cancelled(c)
    assert store.get(task.id).status == "cancelled"


def test_retryable_failure_requeues_with_backoff(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=2)
    c = claim(store)
    assert store.finish_failed(c, "flaky", retryable=True, backoff=backoff)
    got = store.get(task.id)
    assert got.status == "queued"
    assert got.error == "flaky"
    assert got.run_after.timestamp() * 1000 == clock.now + backoff(1)
    assert got.worker_id is None


def test_retryable_failure_exhausts_attempts(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=2)
    for _ in range(2):
        clock.advance(60_000)
        assert store.finish_failed(claim(store), "flaky", retryable=True, backoff=backoff)
    got = store.get(task.id)
    assert got.status == "failed"
    assert got.attempt == 2
    assert got.finished_at is not None


def test_non_retryable_failure_fails_immediately(store: TaskStore) -> None:
    task = store.create("t", "{}", max_attempts=3)
    assert store.finish_failed(claim(store), "bad params", retryable=False, backoff=backoff)
    assert store.get(task.id).status == "failed"


def test_failure_after_cancel_request_is_cancelled(store: TaskStore) -> None:
    task = store.create("t", "{}", max_attempts=3)
    c = claim(store)
    store.request_cancel(task.id)
    assert store.finish_failed(c, "flaky", retryable=True, backoff=backoff)
    assert store.get(task.id).status == "cancelled"


def test_manual_retry_keeps_attempt(store: TaskStore) -> None:
    task = store.create("t", "{}", max_attempts=1)
    assert store.finish_failed(claim(store), "boom", retryable=False, backoff=backoff)
    got = store.retry(task.id, extra_attempts=3)
    assert got.status == "queued"
    assert got.attempt == 1
    assert got.max_attempts == 4
    assert got.error is None
    assert got.finished_at is None
    assert claim(store).attempt == 2


# ---- 租约 ----


def test_renew_extends_lease(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=1)
    c = claim(store)
    clock.advance(LEASE - 1)
    assert store.renew_lease(c, LEASE)
    clock.advance(LEASE - 1)
    assert store.recover_expired(backoff) == []
    assert store.get(task.id).status == "running"


def test_expired_lease_is_recovered_as_retryable(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=3)
    claim(store)
    clock.advance(LEASE + 1)
    assert store.recover_expired(backoff) == [task.id]
    got = store.get(task.id)
    assert got.status == "queued"
    assert got.error is not None
    assert "租约过期" in got.error


def test_expired_lease_on_last_attempt_fails(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=1)
    claim(store)
    clock.advance(LEASE + 1)
    store.recover_expired(backoff)
    assert store.get(task.id).status == "failed"


def test_expired_lease_with_cancel_request_is_cancelled(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=3)
    claim(store)
    store.request_cancel(task.id)
    clock.advance(LEASE + 1)
    store.recover_expired(backoff)
    assert store.get(task.id).status == "cancelled"


def test_stale_worker_cannot_write_after_takeover(store: TaskStore, clock) -> None:
    task = store.create("t", "{}", max_attempts=3)
    old = claim(store, "w-old")
    clock.advance(LEASE + 1)
    store.recover_expired(backoff)
    clock.advance(backoff(1))
    new = claim(store, "w-new")

    assert not store.finish_succeeded(old, '{"by": "old"}')
    assert not store.finish_failed(old, "x", retryable=False, backoff=backoff)
    assert not store.finish_cancelled(old)
    assert not store.update_progress(old, 0.5, "old")
    assert not store.renew_lease(old, LEASE)

    assert store.finish_succeeded(new, '{"by": "new"}')
    got = store.get(task.id)
    assert got.status == "succeeded"
    assert got.result == '{"by": "new"}'


def test_same_worker_old_attempt_cannot_write(store: TaskStore, clock) -> None:
    # 同一个 worker 卡住后又领到了自己的任务：旧的那次执行仍然不能写回。
    store.create("t", "{}", max_attempts=3)
    old = claim(store, "w1")
    clock.advance(LEASE + 1)
    store.recover_expired(backoff)
    clock.advance(backoff(1))
    new = claim(store, "w1")
    assert not store.finish_succeeded(old, None)
    assert store.finish_succeeded(new, None)


# ---- 去重 ----


def test_dedupe_rejects_second_unfinished(store: TaskStore) -> None:
    store.create("t", "{}", max_attempts=1, dedupe_key="k")
    with pytest.raises(DuplicateTaskError):
        store.create("t", "{}", max_attempts=1, dedupe_key="k")


def test_dedupe_allows_after_finish(store: TaskStore) -> None:
    first = store.create("t", "{}", max_attempts=1, dedupe_key="k")
    store.request_cancel(first.id)
    store.create("t", "{}", max_attempts=1, dedupe_key="k")


def test_retry_conflicting_dedupe(store: TaskStore) -> None:
    first = store.create("t", "{}", max_attempts=1, dedupe_key="k")
    store.request_cancel(first.id)
    store.create("t", "{}", max_attempts=1, dedupe_key="k")
    with pytest.raises(DuplicateTaskError):
        store.retry(first.id, extra_attempts=1)
    assert store.get(first.id).status == "cancelled"


# ---- 数据库 ----


def test_sqlite_version_check() -> None:
    with pytest.raises(RuntimeError, match=r"3.35.0"):
        check_sqlite_version((3, 34, 1))
    check_sqlite_version((3, 35, 0))


def test_store_refuses_old_sqlite(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 31, 1))
    with pytest.raises(RuntimeError):
        TaskStore(db)


def test_reopen_keeps_data(db: Path, clock) -> None:
    task = TaskStore(db, clock).create("t", "{}", max_attempts=1)
    assert TaskStore(db, clock).get(task.id).id == task.id


def test_refuses_newer_schema(db: Path, clock) -> None:
    TaskStore(db, clock)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="99"):
        TaskStore(db, clock)


def test_busy_timeout_is_set(store: TaskStore) -> None:
    with store._connect() as conn:  # pyright: ignore[reportPrivateUsage]
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
