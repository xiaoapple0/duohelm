"""定时触发（tasks/schedule.py、api/schedules.py）。时钟全部用假时钟，不做真实等待。

2024-01-02 是星期二，2024-01-06 是星期六。2024-02-12（周一）是春节假期，在日历里缺席。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from duohelm.settings import Settings
from duohelm.storage import DataStore
from duohelm.storage.datasets import CALENDAR_SCHEMA
from duohelm.tasks import TaskStore, TaskTypeRegistry
from duohelm.tasks import store as store_module
from duohelm.tasks.builtin import builtin_task_types
from duohelm.tasks.schedule import (
    DEDUPE_KEY,
    DataState,
    ScheduleError,
    Scheduler,
    ScheduleStore,
    check_run_time,
    is_trading_day,
    parse_run_time,
    read_data_state,
)
from duohelm_sdk.schema import BARS_SCHEMA

SH = timezone(timedelta(hours=8))
NY = timezone(timedelta(hours=-5))  # 本机时区为 UTC-5（美东标准时间）


class FakeNow:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def bj(day: str, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(date.fromisoformat(day), time(h, m), tzinfo=SH)


def trading_days(
    start: date, end: date, holidays: frozenset[date] = frozenset()
) -> frozenset[date]:
    days = (start + timedelta(days=i) for i in range((end - start).days + 1))
    return frozenset(d for d in days if d.weekday() < 5 and d not in holidays)


class Env:
    def __init__(self, tmp_path: Path, at: datetime, calendar: frozenset[date] | None) -> None:
        settings = Settings(home=tmp_path)
        self.tasks = TaskStore(settings.db_path)
        self.types = TaskTypeRegistry(builtin_task_types(settings))
        self.schedules = ScheduleStore(self.tasks)
        self.now = FakeNow(at)
        self.calendar = calendar
        self.has_bars = True
        self.scheduler = self.make_scheduler()

    def make_scheduler(self) -> Scheduler:
        return Scheduler(
            self.schedules,
            self.tasks,
            self.types,
            lambda: DataState(self.calendar, self.has_bars),
            self.now,
        )

    def enable(self, plugin_id: str = "xtdata", run_time: str = "16:00") -> None:
        self.schedules.update(
            now=self.now(), enabled=True, plugin_id=plugin_id, run_time=parse_run_time(run_time)
        )

    def sync_tasks(self):
        return self.tasks.list(type="data.sync")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    # 日历只同步到 2024-01-01（昨天），没有覆盖 2024-01-02
    return Env(
        tmp_path, bj("2024-01-01", "20:00"), trading_days(date(2023, 12, 1), date(2024, 1, 1))
    )


def test_disabled_by_default(env: Env) -> None:
    env.now.at = bj("2024-01-02", "18:00")
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []
    assert env.scheduler.status().next_run_at is None
    cfg = env.schedules.get()
    assert (cfg.enabled, cfg.plugin_id, cfg.run_time) == (False, None, time(16, 0))


def test_triggers_on_trading_day_once_with_incremental_params(env: Env) -> None:
    env.enable()
    env.now.at = bj("2024-01-02", "15:59")
    assert env.scheduler.tick() is None  # 没到时间

    env.now.at = bj("2024-01-02", "16:00")
    task_id = env.scheduler.tick()
    assert task_id is not None
    [task] = env.sync_tasks()
    assert task.id == task_id
    assert task.dedupe_key == DEDUPE_KEY
    assert json.loads(task.params) == {
        "plugin_id": "xtdata",
        # 快照日历的最新日期（2024-01-01）往前 7 个自然日；end 不传，用默认值
        "start": "2023-12-25",
        "end": None,
        "indexes": None,  # 用 data.sync 的默认指数
    }

    # 同一天再检查不会重复提交（即使任务已经结束）
    env.tasks.request_cancel(task_id)
    env.now.at = bj("2024-01-02", "16:05")
    assert env.scheduler.tick() is None
    assert len(env.sync_tasks()) == 1


def test_weekend_does_not_trigger(env: Env) -> None:
    env.enable()
    env.calendar = None
    env.now.at = bj("2024-01-06", "16:30")  # 周六
    assert env.scheduler.tick() is None
    env.now.at = bj("2024-01-07", "16:30")  # 周日
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []


def test_calendar_holiday_does_not_trigger(tmp_path: Path) -> None:
    holiday = date(2024, 2, 12)
    calendar = trading_days(date(2024, 1, 1), date(2024, 2, 14), frozenset({holiday}))
    env = Env(tmp_path, bj("2024-02-12", "10:00"), calendar)
    env.enable()
    env.now.at = bj("2024-02-12", "16:30")  # 周一，但日历覆盖到 2 月 14 日且没有这一天
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []
    env.now.at = bj("2024-02-13", "16:00")  # 周二，日历覆盖到，是交易日
    assert env.scheduler.tick() is not None


def test_calendar_not_covering_today_still_triggers(env: Env) -> None:
    # 日历只到 2024-01-01：没覆盖 2024-01-02，不能当成节假日，照常触发
    assert env.calendar is not None and date(2024, 1, 2) not in env.calendar
    env.enable()
    env.now.at = bj("2024-01-02", "16:00")
    assert env.scheduler.tick() is not None


def test_no_bars_does_not_trigger_and_asks_for_full_sync(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.has_bars = False  # 快照里没有日线（或还没有快照）
    env.enable()
    assert env.scheduler.status().needs_full_sync is True
    env.now.at = bj("2024-01-02", "16:00")
    with caplog.at_level(logging.WARNING, logger="duohelm.tasks.schedule"):
        assert env.scheduler.tick() is None
    assert "全量同步" in caplog.text
    assert env.sync_tasks() == []
    env.has_bars = True
    assert env.scheduler.status().needs_full_sync is False


def test_start_follows_latest_snapshot_date_after_long_outage(tmp_path: Path) -> None:
    # 快照只到 2023-12-01，停机一个多月：start 从快照最新日期往前 7 天，不会漏掉中间的日子
    calendar = trading_days(date(2023, 11, 1), date(2023, 12, 1))
    env = Env(tmp_path, bj("2024-01-15", "10:00"), calendar)
    env.enable()
    env.now.at = bj("2024-01-15", "16:00")
    assert env.scheduler.tick() is not None
    [task] = env.sync_tasks()
    assert json.loads(task.params)["start"] == "2023-11-24"


def test_missed_trigger_runs_once_same_day(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.enable()
    # serve 在 16:00 没运行，17:40 才启动
    env.now.at = bj("2024-01-02", "17:40")
    with caplog.at_level(logging.INFO, logger="duohelm.tasks.schedule"):
        assert env.scheduler.tick() is not None
    assert "补跑" in caplog.text
    assert env.scheduler.tick() is None
    assert len(env.sync_tasks()) == 1


def test_missed_trigger_is_not_replayed_on_later_days(env: Env) -> None:
    env.enable()
    # 1 月 2 日整天没运行；1 月 3 日早上启动，还没到触发时间：不补 1 月 2 日
    env.now.at = bj("2024-01-03", "08:00")
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []
    # 1 月 3 日到点：只触发 1 月 3 日这一次
    env.now.at = bj("2024-01-03", "16:00")
    assert env.scheduler.tick() is not None
    assert len(env.sync_tasks()) == 1
    assert env.schedules.get().last_trigger_date == date(2024, 1, 3)


def test_restart_does_not_trigger_again(env: Env) -> None:
    env.enable()
    env.now.at = bj("2024-01-02", "16:00")
    assert env.scheduler.tick() is not None
    restarted = env.make_scheduler()
    env.now.at = bj("2024-01-02", "19:00")
    assert restarted.tick() is None
    assert len(env.sync_tasks()) == 1


def test_duplicate_is_skipped_and_logged(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.enable()
    # 手动或上一次触发留下了一个未结束的同类任务（同一个 dedupe_key）
    env.tasks.create("data.sync", "{}", max_attempts=3, dedupe_key=DEDUPE_KEY)
    env.now.at = bj("2024-01-02", "16:00")
    with caplog.at_level(logging.INFO, logger="duohelm.tasks.schedule"):
        assert env.scheduler.tick() is None
    assert "跳过" in caplog.text
    assert len(env.sync_tasks()) == 1
    # 这一天已记为触发过：不会每个周期重试
    assert env.schedules.get().last_trigger_date == date(2024, 1, 2)


def test_local_timezone_utc_minus_5_still_uses_beijing_time(env: Env) -> None:
    env.enable()
    # 纽约 1 月 1 日 23:30 = 北京 1 月 2 日 12:30：还没到 16:00
    env.now.at = datetime(2024, 1, 1, 23, 30, tzinfo=NY)
    assert env.scheduler.tick() is None
    # 纽约 1 月 2 日 03:30 = 北京 1 月 2 日 16:30：触发，日期按北京日期算
    env.now.at = datetime(2024, 1, 2, 3, 30, tzinfo=NY)
    assert env.scheduler.tick() is not None
    [task] = env.sync_tasks()
    assert json.loads(task.params)["start"] == "2023-12-25"
    assert env.schedules.get().last_trigger_date == date(2024, 1, 2)
    # 同一个时刻换成 UTC 表示，结果一致
    assert env.now.at.astimezone(UTC).date() == date(2024, 1, 2)


def test_beijing_date_differs_from_local_date_across_midnight(env: Env) -> None:
    env.enable()
    # 北京周六 06:00 = 纽约周五 17:00。本机日期、时刻都满足“周五 16:00 之后”，但北京已是周六，不触发
    env.calendar = None
    env.now.at = datetime(2024, 1, 5, 17, 0, tzinfo=NY)
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []


def test_enabling_after_trigger_time_does_not_fire_immediately(env: Env) -> None:
    env.now.at = bj("2024-01-02", "18:00")
    env.enable()
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []
    env.now.at = bj("2024-01-03", "16:00")
    assert env.scheduler.tick() is not None


@pytest.mark.parametrize("text", ["15:30", "15:00", "09:30", "00:00"])
def test_run_time_not_after_close_is_rejected(text: str) -> None:
    with pytest.raises(ScheduleError, match="15:30"):
        check_run_time(parse_run_time(text))


@pytest.mark.parametrize("text", ["15:31", "16:00", "23:59"])
def test_run_time_after_close_is_accepted(text: str) -> None:
    check_run_time(parse_run_time(text))


@pytest.mark.parametrize("text", ["16", "16:00:00", "4pm", "25:00", "16:0", ""])
def test_run_time_format(text: str) -> None:
    with pytest.raises(ScheduleError):
        parse_run_time(text)


def test_update_rejects_early_time_and_keeps_old_values(env: Env) -> None:
    env.enable(run_time="17:00")
    with pytest.raises(ScheduleError):
        env.schedules.update(now=env.now(), run_time=time(15, 0))
    assert env.schedules.get().run_time == time(17, 0)


def test_update_only_changes_given_fields(env: Env) -> None:
    env.enable(plugin_id="xtdata", run_time="17:30")
    env.schedules.update(now=env.now(), enabled=False)
    cfg = env.schedules.get()
    assert (cfg.enabled, cfg.plugin_id, cfg.run_time) == (False, "xtdata", time(17, 30))
    env.schedules.update(now=env.now(), plugin_id="eltdx")
    cfg = env.schedules.get()
    assert (cfg.enabled, cfg.plugin_id, cfg.run_time) == (False, "eltdx", time(17, 30))


def test_enable_requires_plugin_id(env: Env) -> None:
    with pytest.raises(ScheduleError, match="plugin_id"):
        env.schedules.update(now=env.now(), enabled=True)
    assert env.schedules.get().enabled is False


def test_next_run_at_skips_weekend_and_holiday(tmp_path: Path) -> None:
    holiday = date(2024, 2, 12)
    calendar = trading_days(date(2024, 1, 1), date(2024, 2, 14), frozenset({holiday}))
    env = Env(tmp_path, bj("2024-02-09", "10:00"), calendar)  # 周五
    env.enable()
    assert env.scheduler.status().next_run_at == bj("2024-02-09", "16:00")
    env.now.at = bj("2024-02-09", "17:00")
    env.schedules.mark_triggered(date(2024, 2, 9))
    # 周六、周日不触发；周一是日历里的节假日；下一个是周二
    assert env.scheduler.status().next_run_at == bj("2024-02-13", "16:00")


def test_is_trading_day() -> None:
    cal = trading_days(date(2024, 1, 1), date(2024, 1, 31))
    assert is_trading_day(date(2024, 1, 3), cal)
    assert not is_trading_day(date(2024, 1, 6), cal)  # 周末
    assert is_trading_day(date(2024, 2, 1), cal)  # 日历没覆盖：照常算
    assert not is_trading_day(date(2024, 1, 1), cal - {date(2024, 1, 1)})  # 元旦不在日历里
    assert not is_trading_day(date(2024, 2, 3), None)  # 周六
    assert is_trading_day(date(2024, 2, 2), None)


def test_migration_from_version_1_keeps_tasks(tmp_path: Path) -> None:
    db = tmp_path / "duohelm.db"
    conn = sqlite3.connect(db)
    for statement in store_module._SCHEMA_V1.split(";"):  # pyright: ignore[reportPrivateUsage]
        if statement.strip():
            conn.execute(statement)
    conn.execute(
        "INSERT INTO tasks (type, params, status, max_attempts, run_after, created_at) "
        "VALUES ('plugins.scan', '{}', 'queued', 3, 0, 0)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    store = TaskStore(db)
    assert [t.type for t in store.list()] == ["plugins.scan"]
    assert ScheduleStore(store).get().enabled is False
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store_module.SCHEMA_VERSION
    conn.close()


# ---- 读快照失败是“未知” ----


def test_state_read_failure_is_unknown_and_retried(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.enable()
    broken = {"on": True}

    def reader() -> DataState:
        if broken["on"]:
            raise RuntimeError("清单损坏")
        return DataState(env.calendar, True)

    scheduler = Scheduler(env.schedules, env.tasks, env.types, reader, env.now)
    env.now.at = bj("2024-01-02", "16:00")
    with pytest.raises(RuntimeError):
        scheduler.tick()  # run_forever 会捕获并在下个周期重试
    assert env.sync_tasks() == []
    assert env.schedules.get().last_trigger_date != date(2024, 1, 2)  # 没有记为已处理

    status = scheduler.status()  # 给 API 的状态：未知，不是“需要全量同步”，下次触发时间也未知
    assert status.needs_full_sync is None
    assert status.next_run_at is None
    assert status.state_error is not None and "清单损坏" in status.state_error

    broken["on"] = False
    env.now.at = bj("2024-01-02", "16:00") + timedelta(seconds=30)
    assert scheduler.tick() is not None  # 下个周期恢复后照常触发
    assert scheduler.status().state_error is None


def test_needs_full_sync_also_when_calendar_missing(env: Env) -> None:
    # tick 要求日线和日历都有；needs_full_sync 用同一个判断
    env.calendar = None
    env.enable()
    assert env.scheduler.status().needs_full_sync is True
    env.now.at = bj("2024-01-02", "16:00")
    assert env.scheduler.tick() is None
    assert env.sync_tasks() == []


# ---- 用真实 DataStore 快照读状态 ----


def write_snapshot(data: DataStore, *, bars: bool = True) -> None:
    with data.transaction() as tx:
        if bars:
            tx.write(
                "bars_1d",
                pl.DataFrame(
                    [(date(2024, 1, 3), "600000.SH", 10.0, 11.0, 9.0, 10.5, 1000.0, 10500.0)],
                    schema=BARS_SCHEMA,
                    orient="row",
                ),
            )
        tx.write(
            "calendar",
            pl.DataFrame(
                [
                    ("CN", date(2024, 1, 2)),
                    ("CN", date(2024, 1, 3)),
                    ("HK", date(2024, 1, 4)),  # 别的市场的日历不能算进 CN
                ],
                schema=CALENDAR_SCHEMA,
                orient="row",
            ),
        )
        tx.commit()


def test_read_data_state_from_real_snapshot(tmp_path: Path) -> None:
    data = DataStore(tmp_path / "data")
    assert read_data_state(data) == DataState(None, False)  # 还没有快照
    write_snapshot(data)
    state = read_data_state(data)
    assert state.has_bars
    assert state.calendar == frozenset({date(2024, 1, 2), date(2024, 1, 3)})
    assert state.can_sync


def test_read_data_state_without_bars(tmp_path: Path) -> None:
    data = DataStore(tmp_path / "data")
    write_snapshot(data, bars=False)
    state = read_data_state(data)
    assert not state.has_bars
    assert not state.can_sync


def test_incremental_start_follows_real_snapshot_end_to_end(tmp_path: Path) -> None:
    data = DataStore(tmp_path / "data")
    write_snapshot(data)
    env = Env(tmp_path, bj("2024-01-10", "10:00"), None)
    env.scheduler = Scheduler(
        env.schedules, env.tasks, env.types, lambda: read_data_state(data), env.now
    )
    env.enable()
    env.now.at = bj("2024-01-10", "16:00")
    assert env.scheduler.tick() is not None
    [task] = env.sync_tasks()
    # CN 日历最新日期 2024-01-03 往前 7 天；HK 的 2024-01-04 不能影响它
    assert json.loads(task.params)["start"] == "2023-12-27"


# ---- 只在启用或改时间时把当天记为已处理 ----


def test_update_marks_today_only_when_enabling_or_retiming(env: Env) -> None:
    env.now.at = bj("2024-01-02", "18:00")
    env.schedules.update(now=env.now(), plugin_id="xtdata")  # 没启用：不标记
    assert env.schedules.get().last_trigger_date is None
    env.schedules.update(now=env.now(), enabled=True)  # 启用：标记
    assert env.schedules.get().last_trigger_date == date(2024, 1, 2)

    env.schedules.mark_triggered(date(2024, 1, 1))  # 回到“今天还没处理”
    env.schedules.update(now=env.now())  # 空 PATCH：不动
    assert env.schedules.get().last_trigger_date == date(2024, 1, 1)
    env.schedules.update(now=env.now(), plugin_id="eltdx")  # 只改插件：不动
    assert env.schedules.get().last_trigger_date == date(2024, 1, 1)
    env.schedules.update(now=env.now(), run_time=time(17, 0))  # 改时间且已过：标记
    assert env.schedules.get().last_trigger_date == date(2024, 1, 2)
