"""目录库（SQLite）：快照、当前快照指针、钉住的快照、退市股扫描记录。

快照的内容（每个数据集生效的文件）在清单文件里，这里只记录元数据。切换当前快照时
要求它仍然等于写入开始时的快照，否则说明有别的写入抢先提交了（乐观锁）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from duohelm.tasks.store import Clock, check_sqlite_version, now_ms, to_datetime

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5_000
HEAD = "main"

_SCHEMA_V1 = """
CREATE TABLE snapshots (
  id         TEXT PRIMARY KEY,
  parent     TEXT,
  created_at INTEGER NOT NULL,
  task_id    INTEGER,
  note       TEXT,
  stats      TEXT NOT NULL DEFAULT '{}',
  collected  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE heads (
  name        TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id)
);
CREATE TABLE pins (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
  reason      TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (snapshot_id, reason)
);
CREATE TABLE security_scan (
  symbol     TEXT PRIMARY KEY,
  found      INTEGER NOT NULL,
  scanned_at INTEGER NOT NULL
);
"""

# v2：扫描记录按数据源分开。一个数据源没有某个代码，不代表别的数据源也没有（#30）。
# v1 的表没有任何代码写过，但仍改名保留，不删除。
_SCHEMA_V2 = """
ALTER TABLE security_scan RENAME TO security_scan_v1;
CREATE TABLE security_scan (
  source     TEXT NOT NULL,
  symbol     TEXT NOT NULL,
  found      INTEGER NOT NULL,
  scanned_at INTEGER NOT NULL,
  PRIMARY KEY (source, symbol)
);
"""

# v3：“没找到”的代码按重扫间隔重新查询，依据是 scanned_at（最近一次查询的时间）。v2 之前
# 的行没有可靠的查询时间，而且正是这些行可能把存在的代码误记成了没有（#36、#37），
# 所以把它们的查询时间置 0（未知，视为已过期），下一次同步就重扫。
_SCHEMA_V3 = """
UPDATE security_scan SET scanned_at = 0 WHERE found = 0;
"""


class SnapshotConflictError(RuntimeError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        super().__init__(f"当前快照已从 {expected} 变为 {actual}，写入需要基于新快照重做")


class SnapshotNotFoundError(KeyError):
    def __init__(self, snapshot_id: str) -> None:
        super().__init__(f"快照 {snapshot_id} 不存在或已被清理")


@dataclass(frozen=True)
class SnapshotInfo:
    id: str
    parent: str | None
    created_at: datetime
    task_id: int | None
    note: str | None
    stats: dict[str, Any]
    collected: bool


class Catalog:
    def __init__(self, path: Path, clock: Clock = now_ms) -> None:
        check_sqlite_version()
        self._path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
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
                    f"数据目录库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}，请升级 DuoHelm"
                )
            for target, script in ((1, _SCHEMA_V1), (2, _SCHEMA_V2), (3, _SCHEMA_V3)):
                if version < target:
                    for statement in script.split(";"):
                        if statement.strip():
                            conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---- 快照 ----

    def head(self) -> str | None:
        with self._connect() as conn:
            return self._head(conn)

    def _head(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT snapshot_id FROM heads WHERE name = ?", (HEAD,)).fetchone()
        return None if row is None else str(row["snapshot_id"])

    def advance_head(
        self,
        snapshot_id: str,
        *,
        expected: str | None,
        task_id: int | None = None,
        note: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        """记录快照并把当前指针指向它。当前指针不等于 expected 时抛 SnapshotConflictError。"""
        with self._write() as conn:
            actual = self._head(conn)
            if actual != expected:
                raise SnapshotConflictError(expected, actual)
            conn.execute(
                "INSERT INTO snapshots (id, parent, created_at, task_id, note, stats) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET collected = 0",  # 内容回到了以前的某个快照
                (
                    snapshot_id,
                    expected,
                    self._clock(),
                    task_id,
                    note,
                    json.dumps(stats or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO heads (name, snapshot_id) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET snapshot_id = excluded.snapshot_id",
                (HEAD, snapshot_id),
            )

    def snapshot(self, snapshot_id: str) -> SnapshotInfo:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise SnapshotNotFoundError(snapshot_id)
        return _info(row)

    def snapshots(self, *, limit: int = 50, include_collected: bool = False) -> list[SnapshotInfo]:
        where = "" if include_collected else "WHERE collected = 0"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM snapshots {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_info(r) for r in rows]

    def mark_collected(self, snapshot_ids: Iterable[str]) -> None:
        with self._write() as conn:
            conn.executemany(
                "UPDATE snapshots SET collected = 1 WHERE id = ?", [(i,) for i in snapshot_ids]
            )

    # ---- 钉住：被研究或回测记录引用的快照不会被清理 ----

    def pin(self, snapshot_id: str, reason: str) -> None:
        with self._write() as conn:
            row = conn.execute(
                "SELECT collected FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None or row["collected"]:
                raise SnapshotNotFoundError(snapshot_id)
            conn.execute(
                "INSERT OR IGNORE INTO pins (snapshot_id, reason, created_at) VALUES (?, ?, ?)",
                (snapshot_id, reason, self._clock()),
            )

    def unpin(self, snapshot_id: str, reason: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM pins WHERE snapshot_id = ? AND reason = ?", (snapshot_id, reason)
            )

    def pinned(self) -> set[str]:
        with self._connect() as conn:
            return {str(r[0]) for r in conn.execute("SELECT DISTINCT snapshot_id FROM pins")}

    # ---- 退市股扫描记录：按数据源记录，扫过的代码不再重复扫描 ----

    def record_scan(self, source: str, results: Iterable[tuple[str, bool]]) -> None:
        """记下数据源 source 有没有这些代码，以及查询时间。扫到过（found）的不会再改回没扫到。"""
        now = self._clock()
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO security_scan (source, symbol, found, scanned_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(source, symbol) DO UPDATE SET "
                "found = MAX(found, excluded.found), scanned_at = excluded.scanned_at",
                [(source, symbol, int(found), now) for symbol, found in results],
            )

    def unscanned(self, source: str, symbols: Sequence[str]) -> list[str]:
        """symbols 中数据源 source 还没有扫描过的代码，保持原顺序。"""
        with self._connect() as conn:
            conn.execute("CREATE TEMP TABLE want (symbol TEXT PRIMARY KEY)")
            conn.executemany("INSERT OR IGNORE INTO want VALUES (?)", [(s,) for s in symbols])
            done = {
                str(r[0])
                for r in conn.execute(
                    "SELECT w.symbol FROM want w JOIN security_scan s "
                    "ON s.symbol = w.symbol AND s.source = ?",
                    (source,),
                )
            }
        return [s for s in symbols if s not in done]

    def rescan_due(self, source: str, interval_s: float) -> list[str]:
        """数据源 source 记为“没找到”、且距上次查询已超过 interval_s 秒的代码，按代码排序。

        interval_s 为 0 时，所有“没找到”的代码都到期。查询时间未知（0）的总是到期。
        """
        cutoff = self._clock() - int(interval_s * 1000)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM security_scan "
                "WHERE source = ? AND found = 0 AND scanned_at <= ? ORDER BY symbol",
                (source, cutoff),
            )
            return [str(r[0]) for r in rows]

    def found_symbols(self, source: str) -> list[str]:
        """数据源 source 扫到过（或在代码表里出现过）的代码，按代码排序。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM security_scan WHERE source = ? AND found = 1 ORDER BY symbol",
                (source,),
            )
            return [str(r[0]) for r in rows]

    def scan_summary(self, source: str) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS scanned, COALESCE(SUM(found), 0) AS found "
                "FROM security_scan WHERE source = ?",
                (source,),
            ).fetchone()
        return {"scanned": int(row["scanned"]), "found": int(row["found"])}


def _info(row: sqlite3.Row) -> SnapshotInfo:
    return SnapshotInfo(
        id=str(row["id"]),
        parent=row["parent"],
        created_at=to_datetime(row["created_at"]),
        task_id=row["task_id"],
        note=row["note"],
        stats=json.loads(row["stats"]),
        collected=bool(row["collected"]),
    )
