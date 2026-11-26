"""任务表的存储层：SQLite 读写、状态流转、租约。

设计见 docs/subsystems/tasks.md。所有状态变化都经过本模块，状态流转规则集中在
``ALLOWED_TRANSITIONS``；worker 写回结果时必须同时匹配 task id、worker id、attempt
和 running 状态，租约被接管后的写回一律失败。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
LogLevel = Literal["debug", "info", "warning", "error"]
Clock = Callable[[], int]
"""返回当前 UTC 时间的毫秒数。测试注入假时钟。"""
Backoff = Callable[[int], int]
"""已执行次数 -> 下一次重试前等待的毫秒数。"""

MIN_SQLITE_VERSION = (3, 35, 0)  # UPDATE ... RETURNING
SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5_000

ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    "queued": frozenset({"running", "cancelled"}),
    # running -> queued：可重试的失败或 worker 中断，等待下一次执行
    "running": frozenset({"queued", "succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    # failed / cancelled -> queued：手动重试
    "failed": frozenset({"queued"}),
    "cancelled": frozenset({"queued"}),
}
UNFINISHED: frozenset[TaskStatus] = frozenset({"queued", "running"})
MANUAL_RETRY_FROM: frozenset[TaskStatus] = frozenset({"failed", "cancelled"})

_SCHEMA_V1 = """
CREATE TABLE tasks (
  id               INTEGER PRIMARY KEY,
  type             TEXT    NOT NULL,
  params           TEXT    NOT NULL DEFAULT '{}',
  status           TEXT    NOT NULL,
  progress         REAL,
  progress_message TEXT,
  attempt          INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL,
  run_after        INTEGER NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_id        TEXT,
  lease_expires_at INTEGER,
  error            TEXT,
  result           TEXT,
  dedupe_key       TEXT,
  created_at       INTEGER NOT NULL,
  started_at       INTEGER,
  finished_at      INTEGER
);
CREATE INDEX tasks_claim ON tasks(status, run_after);
CREATE UNIQUE INDEX tasks_dedupe ON tasks(dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
CREATE TABLE task_logs (
  id      INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL,
  ts      INTEGER NOT NULL,
  level   TEXT    NOT NULL,
  message TEXT    NOT NULL
);
CREATE INDEX task_logs_task ON task_logs(task_id, id);
"""

# 版本 2：定时规则（见 tasks/schedule.py）。只新建一张表，不动版本 1 的数据。
_SCHEMA_V2 = """
CREATE TABLE schedules (
  name              TEXT PRIMARY KEY,
  enabled           INTEGER NOT NULL DEFAULT 0,
  plugin_id         TEXT,
  run_time          TEXT    NOT NULL,
  last_trigger_date TEXT,
  updated_at        INTEGER NOT NULL
);
"""


class TaskStoreError(Exception):
    """任务存储层错误的基类。"""


class TaskNotFoundError(TaskStoreError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"任务 {task_id} 不存在")


class InvalidTransitionError(TaskStoreError):
    def __init__(self, task_id: int, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(f"任务 {task_id} 当前状态为 {current}，不能变为 {target}")
        self.current = current
        self.target = target


class DuplicateTaskError(TaskStoreError):
    def __init__(self, dedupe_key: str) -> None:
        super().__init__(f"已有未结束的任务使用 dedupe_key={dedupe_key!r}")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def check_sqlite_version(version: tuple[int, int, int] | None = None) -> None:
    """SQLite 版本低于 3.35 时不支持 UPDATE ... RETURNING，任务领取无法保证原子。"""
    actual = version if version is not None else sqlite3.sqlite_version_info
    if actual < MIN_SQLITE_VERSION:
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        have = ".".join(map(str, actual))
        raise RuntimeError(f"任务系统需要 SQLite {need} 或更高版本，当前为 {have}")


@dataclass(frozen=True)
class TaskRecord:
    id: int
    type: str
    params: str
    """JSON 文本。"""
    status: TaskStatus
    progress: float | None
    progress_message: str | None
    attempt: int
    max_attempts: int
    run_after: datetime
    cancel_requested: bool
    worker_id: str | None
    lease_expires_at: datetime | None
    error: str | None
    result: str | None
    """JSON 文本；成功时才有。"""
    dedupe_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskRecord:
        def dt(value: int | None) -> datetime | None:
            return None if value is None else to_datetime(value)

        return cls(
            id=row["id"],
            type=row["type"],
            params=row["params"],
            status=row["status"],
            progress=row["progress"],
            progress_message=row["progress_message"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            run_after=to_datetime(row["run_after"]),
            cancel_requested=bool(row["cancel_requested"]),
            worker_id=row["worker_id"],
            lease_expires_at=dt(row["lease_expires_at"]),
            error=row["error"],
            result=row["result"],
            dedupe_key=row["dedupe_key"],
            created_at=to_datetime(row["created_at"]),
            started_at=dt(row["started_at"]),
            finished_at=dt(row["finished_at"]),
        )


@dataclass(frozen=True)
class TaskLog:
    id: int
    task_id: int
    attempt: int
    ts: datetime
    level: LogLevel
    message: str


@dataclass(frozen=True)
class Claim:
    """worker 领取到的一次执行。写回时用它证明自己仍然持有这个任务。"""

    task: TaskRecord
    worker_id: str

    @property
    def task_id(self) -> int:
        return self.task.id

    @property
    def attempt(self) -> int:
        return self.task.attempt


_OWNED = "id = :id AND worker_id = :worker_id AND attempt = :attempt AND status = 'running'"


def _owned(claim: Claim) -> dict[str, object]:
    return {"id": claim.task_id, "worker_id": claim.worker_id, "attempt": claim.attempt}


class TaskStore:
    def __init__(self, path: Path, clock: Clock = now_ms) -> None:
        check_sqlite_version()
        self._path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @property
    def path(self) -> Path:
        """数据库文件。定时规则表（tasks/schedule.py）和任务表在同一个库里。"""
        return self._path

    # ---- 连接 ----

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        # 每次操作一个连接：worker 线程、心跳线程和 API 线程互不共享连接。
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        """写事务一开始就拿写锁，避免读后升级写锁时的死锁。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        with self._write() as conn:
            version: int = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，请升级 DuoHelm"
                )
            if version < 1:
                for statement in _SCHEMA_V1.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            if version < 2:
                conn.execute(_SCHEMA_V2)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---- 读 ----

    def get(self, task_id: int) -> TaskRecord:
        with self._connect() as conn:
            return self._get(conn, task_id)

    def _get(self, conn: sqlite3.Connection, task_id: int) -> TaskRecord:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return TaskRecord.from_row(row)

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        type: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[TaskRecord]:
        """按 id 倒序。before_id 是游标：只返回 id 小于它的任务。"""
        clauses: list[str] = []
        args: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if type is not None:
            clauses.append("type = ?")
            args.append(type)
        if before_id is not None:
            clauses.append("id < ?")
            args.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", args)
            return [TaskRecord.from_row(r) for r in rows]

    def logs(self, task_id: int, *, after_id: int = 0, limit: int = 500) -> list[TaskLog]:
        with self._connect() as conn:
            self._get(conn, task_id)
            rows = conn.execute(
                "SELECT * FROM task_logs WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
                (task_id, after_id, limit),
            )
            return [
                TaskLog(
                    id=r["id"],
                    task_id=r["task_id"],
                    attempt=r["attempt"],
                    ts=to_datetime(r["ts"]),
                    level=r["level"],
                    message=r["message"],
                )
                for r in rows
            ]

    def is_cancel_requested(self, task_id: int) -> bool:
        with self._connect() as conn:
            return self._get(conn, task_id).cancel_requested

    # ---- 创建、取消、手动重试 ----

    def create(
        self,
        type: str,
        params: str,
        *,
        max_attempts: int,
        dedupe_key: str | None = None,
    ) -> TaskRecord:
        if max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        now = self._clock()
        try:
            with self._write() as conn:
                cur = conn.execute(
                    "INSERT INTO tasks (type, params, status, max_attempts, run_after, "
                    "dedupe_key, created_at) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
                    (type, params, max_attempts, now, dedupe_key, now),
                )
                task_id = cur.lastrowid
                assert task_id is not None
                return self._get(conn, task_id)
        except sqlite3.IntegrityError as exc:
            if dedupe_key is not None:
                raise DuplicateTaskError(dedupe_key) from exc
            raise

    def request_cancel(self, task_id: int) -> TaskRecord:
        """排队中的任务直接取消；运行中的任务只做标记，由 handler 在检查点退出。"""
        now = self._clock()
        with self._write() as conn:
            task = self._get(conn, task_id)
            if task.status == "queued":
                conn.execute(
                    "UPDATE tasks SET status = 'cancelled', cancel_requested = 1, "
                    "finished_at = ? WHERE id = ?",
                    (now, task_id),
                )
                self._log(conn, task_id, task.attempt, "info", "任务在排队中被取消")
            elif task.status == "running":
                conn.execute("UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,))
                self._log(conn, task_id, task.attempt, "info", "已请求取消，等待任务退出")
            else:
                raise InvalidTransitionError(task_id, task.status, "cancelled")
            return self._get(conn, task_id)

    def retry(self, task_id: int, *, extra_attempts: int) -> TaskRecord:
        """手动重试。attempt 不清零，再给 extra_attempts 次机会。"""
        if extra_attempts < 1:
            raise ValueError("extra_attempts 至少为 1")
        now = self._clock()
        with self._write() as conn:
            task = self._get(conn, task_id)
            # 流转表允许 running -> queued（自动重试），手动重试只接受已结束的失败或取消。
            if task.status not in MANUAL_RETRY_FROM:
                raise InvalidTransitionError(task_id, task.status, "queued")
            try:
                conn.execute(
                    "UPDATE tasks SET status = 'queued', max_attempts = attempt + ?, "
                    "run_after = ?, cancel_requested = 0, worker_id = NULL, "
                    "lease_expires_at = NULL, progress = NULL, progress_message = NULL, "
                    "error = NULL, result = NULL, finished_at = NULL WHERE id = ?",
                    (extra_attempts, now, task_id),
                )
            except sqlite3.IntegrityError as exc:
                # 同一个 dedupe_key 已有未结束的任务
                raise DuplicateTaskError(task.dedupe_key or "") from exc
            self._log(conn, task_id, task.attempt, "info", "手动重试")
            return self._get(conn, task_id)

    # ---- worker 使用 ----

    def claim(self, worker_id: str, lease_ms: int) -> Claim | None:
        """原子地领取最早的一个可运行任务。没有就返回 None。"""
        now = self._clock()
        with self._write() as conn:
            rows = conn.execute(
                "UPDATE tasks SET status = 'running', worker_id = ?, lease_expires_at = ?, "
                "attempt = attempt + 1, started_at = ?, progress = NULL, "
                "progress_message = NULL "
                "WHERE id = (SELECT id FROM tasks WHERE status = 'queued' AND run_after <= ? "
                "ORDER BY id LIMIT 1) AND status = 'queued' "
                "RETURNING *",
                (worker_id, now + lease_ms, now, now),
            ).fetchall()  # 取完结果，语句结束后才能 COMMIT
            if not rows:
                return None
            task = TaskRecord.from_row(rows[0])
            self._log(conn, task.id, task.attempt, "info", f"第 {task.attempt} 次执行开始")
            return Claim(task=task, worker_id=worker_id)

    def renew_lease(self, claim: Claim, lease_ms: int) -> bool:
        """心跳续期。返回 False 说明任务已经不属于这次执行。"""
        with self._write() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET lease_expires_at = :lease WHERE {_OWNED}",
                {**_owned(claim), "lease": self._clock() + lease_ms},
            )
            return cur.rowcount == 1

    def update_progress(self, claim: Claim, progress: float | None, message: str | None) -> bool:
        with self._write() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET progress = :progress, progress_message = :message "
                f"WHERE {_OWNED}",
                {**_owned(claim), "progress": progress, "message": message},
            )
            return cur.rowcount == 1

    def append_log(self, task_id: int, attempt: int, level: LogLevel, message: str) -> None:
        with self._write() as conn:
            self._log(conn, task_id, attempt, level, message)

    def finish_succeeded(self, claim: Claim, result: str | None) -> bool:
        now = self._clock()
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'succeeded', result = :result, progress = 1.0, "
                f"finished_at = :now, worker_id = NULL, lease_expires_at = NULL WHERE {_OWNED}",
                {**_owned(claim), "result": result, "now": now},
            )
            if cur.rowcount != 1:
                return False
            self._log(conn, claim.task_id, claim.attempt, "info", "执行成功")
            return True

    def finish_cancelled(self, claim: Claim) -> bool:
        now = self._clock()
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = :now, worker_id = NULL, "
                f"lease_expires_at = NULL WHERE {_OWNED}",
                {**_owned(claim), "now": now},
            )
            if cur.rowcount != 1:
                return False
            self._log(conn, claim.task_id, claim.attempt, "info", "任务已取消")
            return True

    def finish_failed(self, claim: Claim, error: str, *, retryable: bool, backoff: Backoff) -> bool:
        """可重试且还有次数时回到 queued，否则 failed。已请求取消的任务直接 cancelled。"""
        with self._write() as conn:
            row = conn.execute(f"SELECT * FROM tasks WHERE {_OWNED}", _owned(claim)).fetchone()
            if row is None:
                return False
            self._fail(conn, TaskRecord.from_row(row), error, retryable=retryable, backoff=backoff)
            return True

    def recover_expired(self, backoff: Backoff) -> list[int]:
        """把租约已过期的 running 任务当作一次可重试的失败（worker 中断）。"""
        now = self._clock()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' AND lease_expires_at < ?", (now,)
            ).fetchall()
            recovered: list[int] = []
            for row in rows:
                task = TaskRecord.from_row(row)
                self._fail(conn, task, "worker 中断：租约过期", retryable=True, backoff=backoff)
                recovered.append(task.id)
            return recovered

    # ---- 内部 ----

    def _fail(
        self,
        conn: sqlite3.Connection,
        task: TaskRecord,
        error: str,
        *,
        retryable: bool,
        backoff: Backoff,
    ) -> None:
        now = self._clock()
        if task.cancel_requested:
            target: TaskStatus = "cancelled"
        elif retryable and task.attempt < task.max_attempts:
            target = "queued"
        else:
            target = "failed"
        _check(task, target)
        clear = "worker_id = NULL, lease_expires_at = NULL"
        if target == "queued":
            delay = backoff(task.attempt)
            conn.execute(
                f"UPDATE tasks SET status = 'queued', error = ?, run_after = ?, {clear} "
                "WHERE id = ?",
                (error, now + delay, task.id),
            )
            message = f"第 {task.attempt} 次执行失败，{delay // 1000} 秒后重试：{error}"
            self._log(conn, task.id, task.attempt, "warning", message)
        else:
            conn.execute(
                f"UPDATE tasks SET status = ?, error = ?, finished_at = ?, {clear} WHERE id = ?",
                (target, error, now, task.id),
            )
            if target == "cancelled":
                self._log(conn, task.id, task.attempt, "info", f"任务已取消（{error}）")
            else:
                self._log(conn, task.id, task.attempt, "error", f"执行失败：{error}")

    def _log(
        self, conn: sqlite3.Connection, task_id: int, attempt: int, level: LogLevel, message: str
    ) -> None:
        conn.execute(
            "INSERT INTO task_logs (task_id, attempt, ts, level, message) VALUES (?, ?, ?, ?, ?)",
            (task_id, attempt, self._clock(), level, message),
        )


def _check(task: TaskRecord, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[task.status]:
        raise InvalidTransitionError(task.id, task.status, target)
