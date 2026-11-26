"""定时触发：每个交易日 HH:MM（Asia/Shanghai）自动提交一次增量 ``data.sync``。

设计见 docs/subsystems/tasks.md 的“定时触发”。要点：

- 只支持“每个交易日 HH:MM”，时间必须晚于 15:30（盘中的日线不能入库）；默认关闭
- 所有日期、时刻都按 Asia/Shanghai 换算，不依赖本机时区；时钟可注入
- 周末不触发；快照的 CN 日历覆盖到今天时按日历判断，没覆盖时照常触发，由 data.sync 判断
- 错过的触发只在同一天补跑一次；靠 ``last_trigger_date`` 保证每个交易日最多触发一次
- 已有未结束的同类任务时靠 ``dedupe_key`` 跳过
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from duohelm.storage import DataStore
from duohelm.tasks.store import BUSY_TIMEOUT_MS, DuplicateTaskError, TaskStore, now_ms
from duohelm.tasks.sync import CLOSE, MARKET, SHANGHAI
from duohelm.tasks.types import TaskTypeRegistry

logger = logging.getLogger(__name__)

SCHEDULE_NAME = "data.sync"
DEDUPE_KEY = "schedule:data.sync"
DEFAULT_TIME = time(16, 0)
INCREMENTAL_WINDOW_DAYS = 30
"""增量同步的 start：触发当天往前多少个自然日。"""
CHECK_INTERVAL_S = 30.0
LOOKAHEAD_DAYS = 14

Now = Callable[[], datetime]
"""返回当前时间（带时区）。测试注入假时钟。"""
CalendarReader = Callable[[], frozenset[date] | None]
"""CN 交易日历；读不到（还没有快照、读取失败）时为 None。"""


def beijing_now() -> datetime:
    return datetime.now(SHANGHAI)


class ScheduleError(ValueError):
    """规则不合法。API 返回 422。"""


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool
    plugin_id: str | None
    run_time: time
    last_trigger_date: date | None


DEFAULT_CONFIG = ScheduleConfig(
    enabled=False, plugin_id=None, run_time=DEFAULT_TIME, last_trigger_date=None
)


def parse_run_time(text: str) -> time:
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleError(f"时间格式应为 HH:MM，当前为 {text!r}") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond or len(text) != 5:
        raise ScheduleError(f"时间格式应为 HH:MM，当前为 {text!r}")
    return parsed


def check_run_time(run_time: time) -> None:
    if run_time <= CLOSE:
        raise ScheduleError(
            f"触发时间必须晚于 {CLOSE:%H:%M}（北京时间）：盘中的日线不能入库，"
            f"当天的日线和快照只在 {CLOSE:%H:%M} 之后才写，当前为 {run_time:%H:%M}"
        )


class ScheduleStore:
    """定时规则，存在任务库（duohelm.db）的 schedules 表里。表由 TaskStore 的迁移创建。"""

    def __init__(self, tasks: TaskStore) -> None:
        self._path = tasks.path

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _read(conn: sqlite3.Connection) -> ScheduleConfig:
        row = conn.execute("SELECT * FROM schedules WHERE name = ?", (SCHEDULE_NAME,)).fetchone()
        if row is None:
            return DEFAULT_CONFIG
        last = row["last_trigger_date"]
        return ScheduleConfig(
            enabled=bool(row["enabled"]),
            plugin_id=row["plugin_id"],
            run_time=time.fromisoformat(row["run_time"]),
            last_trigger_date=date.fromisoformat(last) if last else None,
        )

    @staticmethod
    def _write(conn: sqlite3.Connection, cfg: ScheduleConfig) -> None:
        conn.execute(
            "INSERT INTO schedules (name, enabled, plugin_id, run_time, last_trigger_date, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "enabled = excluded.enabled, plugin_id = excluded.plugin_id, "
            "run_time = excluded.run_time, last_trigger_date = excluded.last_trigger_date, "
            "updated_at = excluded.updated_at",
            (
                SCHEDULE_NAME,
                int(cfg.enabled),
                cfg.plugin_id,
                f"{cfg.run_time:%H:%M}",
                cfg.last_trigger_date.isoformat() if cfg.last_trigger_date else None,
                now_ms(),
            ),
        )

    def get(self) -> ScheduleConfig:
        with self._connect() as conn:
            return self._read(conn)

    def update(
        self,
        *,
        now: datetime,
        enabled: bool | None = None,
        plugin_id: str | None = None,
        run_time: time | None = None,
    ) -> ScheduleConfig:
        """只改传入的字段，其余保持用户已设的值。

        结果是启用状态、且当天已经过了触发时间时，把当天记为已处理：启用或改时间不会
        立即补跑，从下一个交易日开始触发。
        """
        current = now.astimezone(SHANGHAI)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._read(conn)
                new = ScheduleConfig(
                    enabled=old.enabled if enabled is None else enabled,
                    plugin_id=old.plugin_id if plugin_id is None else plugin_id.strip(),
                    run_time=old.run_time if run_time is None else run_time,
                    last_trigger_date=old.last_trigger_date,
                )
                check_run_time(new.run_time)
                if new.enabled and not new.plugin_id:
                    raise ScheduleError("启用定时同步需要指定行情源插件 id（plugin_id）")
                if new.enabled and current.time() >= new.run_time:
                    new = ScheduleConfig(new.enabled, new.plugin_id, new.run_time, current.date())
                self._write(conn, new)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return new

    def mark_triggered(self, day: date) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._write(conn, _with_last(self._read(conn), day))
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")


def _with_last(cfg: ScheduleConfig, day: date) -> ScheduleConfig:
    return ScheduleConfig(cfg.enabled, cfg.plugin_id, cfg.run_time, day)


def is_trading_day(day: date, calendar: frozenset[date] | None) -> bool:
    """周末不是；日历覆盖到 day 时按日历判断；没覆盖到时照常算（交给 data.sync 判断）。"""
    if day.weekday() >= 5:
        return False
    if calendar and max(calendar) >= day:
        return day in calendar
    return True


def read_cn_calendar(data: DataStore) -> frozenset[date] | None:
    try:
        if data.head() is None:
            return None
        df = data.read("calendar")
        days = df.filter(df["market"] == MARKET)["date"].to_list()
    except Exception:
        # 读不到是“不知道”，不是“不是交易日”：按没覆盖处理。
        logger.warning("读取交易日历失败，按日历未覆盖处理", exc_info=True)
        return None
    return frozenset(days) if days else None


class Scheduler:
    def __init__(
        self,
        schedules: ScheduleStore,
        tasks: TaskStore,
        types: TaskTypeRegistry,
        calendar: CalendarReader,
        now: Now = beijing_now,
    ) -> None:
        self._schedules = schedules
        self._tasks = tasks
        self._types = types
        self._calendar = calendar
        self._now = now

    def now(self) -> datetime:
        return self._now().astimezone(SHANGHAI)

    def tick(self) -> int | None:
        """检查一次，需要触发时提交任务。返回新任务的 id；没有触发或被去重跳过时为 None。"""
        cfg = self._schedules.get()
        if not cfg.enabled or not cfg.plugin_id:
            return None
        current = self._now().astimezone(SHANGHAI)
        today = current.date()
        if cfg.last_trigger_date == today or current.time() < cfg.run_time:
            return None
        if not is_trading_day(today, self._calendar()):
            # 非交易日当天不会再变成交易日，记为已处理，避免每个周期都读日历。
            self._schedules.mark_triggered(today)
            logger.info("%s 不是交易日，不触发定时同步", today)
            return None
        scheduled = datetime.combine(today, cfg.run_time, tzinfo=SHANGHAI)
        if current - scheduled > timedelta(seconds=2 * CHECK_INTERVAL_S):
            logger.info(
                "错过了 %s %s 的定时同步（serve 没有运行或检查被延迟），现在补跑一次",
                today,
                f"{cfg.run_time:%H:%M}",
            )
        start = today - timedelta(days=INCREMENTAL_WINDOW_DAYS)
        params = self._types.validate_params(
            "data.sync", {"plugin_id": cfg.plugin_id, "start": start.isoformat()}
        )
        try:
            task = self._tasks.create(
                "data.sync",
                params,
                max_attempts=self._types.get("data.sync").max_attempts,
                dedupe_key=DEDUPE_KEY,
            )
        except DuplicateTaskError:
            logger.info("已有未结束的 data.sync 任务，跳过 %s 的定时触发", today)
            self._schedules.mark_triggered(today)
            return None
        self._schedules.mark_triggered(today)
        logger.info("定时同步：已提交任务 %s（%s，start=%s）", task.id, cfg.plugin_id, start)
        return task.id

    def next_run_at(self) -> datetime | None:
        """下次触发时间（北京时间）。未启用时为 None。日历没覆盖的未来日子只能按周末判断。"""
        cfg = self._schedules.get()
        if not cfg.enabled or not cfg.plugin_id:
            return None
        current = self._now().astimezone(SHANGHAI)
        calendar = self._calendar()
        for offset in range(LOOKAHEAD_DAYS + 1):
            day = current.date() + timedelta(days=offset)
            if day == cfg.last_trigger_date:
                continue
            if is_trading_day(day, calendar):
                return datetime.combine(day, cfg.run_time, tzinfo=SHANGHAI)
        return None

    def run_forever(self, stop: threading.Event) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("定时触发检查失败，下个周期重试")
            if stop.wait(CHECK_INTERVAL_S):
                return
