"""ST 名单和指数成分快照的拍摄时间 observed_at 与可用日期（#54）。只用 tmp_path。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from duohelm.storage import DataStore
from duohelm.storage import datasets as datasets_module
from duohelm.storage.datasets import OBSERVED_AT, Dataset
from duohelm.storage.reader import DataNotFoundError, MarketReader
from duohelm.tasks.sync import _keep_first_observed  # pyright: ignore[reportPrivateUsage]

SH = timezone(timedelta(hours=8))
# 2024 年：1 月 3、4、5（周五）、8（周一）；春节前最后一个交易日 2 月 8 日（周四），节后 2 月 19 日
CAL = [date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)]
CAL += [date(2024, 2, 8), date(2024, 2, 19)]


def local(series: pl.Series) -> list[datetime | None]:
    """带时区的列转成北京时间的无时区时间再取出（Windows 上 Python 没有 tzdata，不能直接取）。"""
    return series.dt.replace_time_zone(None).to_list()


def naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2024, month, day, hour, minute, tzinfo=SH)


def st_rows(day: date, observed: datetime | None, symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [day] * len(symbols),
            "symbol": symbols,
            "name": ["ST" + s for s in symbols],
            "status": ["st"] * len(symbols),
            "observed_at": [observed] * len(symbols),
        },
        schema=datasets_module.ST_DAILY_SCHEMA,
    )


def member_rows(
    day: date, observed: datetime | None, index: str, symbols: list[str]
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [day] * len(symbols),
            "index": [index] * len(symbols),
            "symbol": symbols,
            "observed_at": [observed] * len(symbols),
        },
        schema=datasets_module.INDEX_MEMBERS_DAILY_SCHEMA,
    )


@pytest.fixture
def store(tmp_path: Path) -> DataStore:
    store = DataStore(tmp_path / "data")
    calendar = pl.DataFrame(
        {"market": ["CN"] * len(CAL), "date": CAL}, schema=datasets_module.CALENDAR_SCHEMA
    )
    with store.transaction() as tx:
        tx.write("calendar", calendar)
    return store


def put(
    store: DataStore, st: pl.DataFrame | None = None, members: pl.DataFrame | None = None
) -> None:
    with store.transaction() as tx:
        if st is not None:
            tx.write("st_daily", st)
        if members is not None:
            tx.write("index_members_daily", members)


def test_after_close_snapshot_not_usable_same_day_but_next_trading_day(store: DataStore) -> None:
    """周四 15:30 之后拍的：周四查不到（拍摄当天），周五查得到。"""
    put(store, st_rows(date(2024, 1, 4), at(1, 4, 15, 40), ["000001.SZ"]))
    reader = MarketReader(store)
    with pytest.raises(DataNotFoundError):
        reader.st_on(date(2024, 1, 4))
    assert reader.st_on(date(2024, 1, 5)).get_column("symbol").to_list() == ["000001.SZ"]


def test_friday_night_usable_on_monday_only(store: DataStore) -> None:
    put(store, st_rows(date(2024, 1, 5), at(1, 5, 21), ["000001.SZ"]))
    reader = MarketReader(store)
    assert reader.st_on(date(2024, 1, 8)).height == 1
    with pytest.raises(DataNotFoundError):
        reader.st_on(date(2024, 1, 5))  # 拍摄当天
    with pytest.raises(DataNotFoundError):
        reader.st_on(date(2024, 1, 6))  # 周六不是交易日


def test_before_holiday_usable_after_holiday(store: DataStore) -> None:
    put(store, members=member_rows(date(2024, 2, 8), at(2, 8, 16), "000300.SH", ["600000.SH"]))
    reader = MarketReader(store)
    assert reader.index_members_on("000300.SH", date(2024, 2, 19)).height == 1
    with pytest.raises(DataNotFoundError):
        reader.index_members_on("000300.SH", date(2024, 2, 8))
    with pytest.raises(DataNotFoundError):
        reader.index_members_on("000905.SH", date(2024, 2, 19))  # 别的指数没有快照


def test_missed_day_is_unknown_not_carried_forward(store: DataStore) -> None:
    """周四的快照不顺延到周一：周五漏跑，周一的名单就是“不知道”。"""
    put(store, st_rows(date(2024, 1, 4), at(1, 4, 16), ["000001.SZ"]))
    with pytest.raises(DataNotFoundError):
        MarketReader(store).st_on(date(2024, 1, 8))


def test_old_rows_without_observed_at_use_snapshot_date(
    store: DataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧文件没有 observed_at 列：不改写、读出来为 null，按快照日期之后的下一个交易日可用；
    和新格式的文件混在同一个数据集里也能读。"""
    old = {k: v for k, v in datasets_module.ST_DAILY_SCHEMA.items() if k != "observed_at"}
    with monkeypatch.context() as m:
        legacy = Dataset("st_daily", old, ("date", "symbol"), "date")
        m.setitem(datasets_module.DATASETS, "st_daily", legacy)
        put(store, st_rows(date(2024, 1, 4), None, ["000001.SZ"]).drop("observed_at"))
    assert local(store.read("st_daily").get_column("observed_at")) == [None]
    put(store, st_rows(date(2024, 1, 5), at(1, 5, 20), ["600000.SH"]))
    reader = MarketReader(store)
    with pytest.raises(DataNotFoundError):
        reader.st_on(date(2024, 1, 4))
    assert reader.st_on(date(2024, 1, 5)).get_column("symbol").to_list() == ["000001.SZ"]
    assert reader.st_on(date(2024, 1, 8)).get_column("symbol").to_list() == ["600000.SH"]


def test_observed_at_is_converted_to_shanghai(store: DataStore) -> None:
    """UTC 1 月 4 日 17:00 是北京时间 1 月 5 日 01:00：拍摄日期是 5 日，8 日才可用。"""
    utc = datetime(2024, 1, 4, 17, 0, tzinfo=UTC)
    put(store, st_rows(date(2024, 1, 4), utc, ["000001.SZ"]))
    reader = MarketReader(store)
    with pytest.raises(DataNotFoundError):
        reader.st_on(date(2024, 1, 5))
    assert reader.st_on(date(2024, 1, 8)).height == 1


def test_rerun_keeps_first_observed_at_when_content_unchanged() -> None:
    first, later = at(1, 5, 16), at(1, 5, 22)
    existing = st_rows(date(2024, 1, 5), first, ["000001.SZ", "600000.SH"])
    renamed = st_rows(date(2024, 1, 5), later, ["000001.SZ"]).with_columns(
        pl.lit("*ST新名").alias("name")
    )
    assert local(_keep_first_observed(existing, renamed).get_column("observed_at")) == [
        naive(later)
    ]
    same = _keep_first_observed(existing, st_rows(date(2024, 1, 5), later, ["600000.SH"]))
    assert local(same.get_column("observed_at")) == [naive(first)]
    legacy = existing.with_columns(pl.lit(None, dtype=OBSERVED_AT).alias("observed_at"))
    assert local(
        _keep_first_observed(legacy, st_rows(date(2024, 1, 5), later, ["600000.SH"])).get_column(
            "observed_at"
        )
    ) == [naive(later)]


def test_partial_date_is_unusable_as_a_whole(store: DataStore) -> None:
    """同一个快照日期里只要有一行不满足，整个日期不可用，不返回残缺的名单。"""
    rows = pl.concat(
        [
            st_rows(date(2024, 1, 4), at(1, 4, 16), ["000001.SZ"]),
            st_rows(date(2024, 1, 4), at(1, 5, 9), ["600000.SH"]),  # 5 日早上补拍的
        ]
    )
    put(store, rows)
    with pytest.raises(DataNotFoundError):
        MarketReader(store).st_on(date(2024, 1, 5))
    # 同样的行拍摄日期都在 4 日：整体可用，两行都在
    put(store, st_rows(date(2024, 1, 4), at(1, 4, 17), ["600000.SH"]))
    assert MarketReader(store).st_on(date(2024, 1, 5)).height == 2


def test_partial_index_members_date_is_unusable(store: DataStore) -> None:
    rows = pl.concat(
        [
            member_rows(date(2024, 1, 4), at(1, 4, 16), "000300.SH", ["600000.SH"]),
            member_rows(date(2024, 1, 4), at(1, 5, 9), "000300.SH", ["000001.SZ"]),
        ]
    )
    put(store, members=rows)
    with pytest.raises(DataNotFoundError):
        MarketReader(store).index_members_on("000300.SH", date(2024, 1, 5))


@pytest.mark.parametrize("missing", ["status", "symbol", "date", "_deleted"])
def test_missing_non_added_column_is_an_error(
    store: DataStore, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """只有声明为“后来新增”的 observed_at 缺失时补 null；缺少别的列（主键、_deleted）报错，
    不能把整个文件的行静默过滤掉。"""
    full = datasets_module.get_dataset("st_daily")
    put(store, st_rows(date(2024, 1, 4), at(1, 4, 16), ["000001.SZ"]))
    entry = store.manifest(store.head()).datasets["st_daily"][0]
    path = store.files_dir / entry.path
    pl.read_parquet(path).drop(missing).write_parquet(path)  # 破坏文件：去掉一列
    assert full.added_columns == ("observed_at",)
    with pytest.raises(pl.exceptions.PolarsError):
        store.read("st_daily")


def test_extra_column_in_file_is_dropped(store: DataStore) -> None:
    """文件里多出来、schema 没有的列读取时丢弃，不影响生效的数据。"""
    put(store, st_rows(date(2024, 1, 4), at(1, 4, 16), ["000001.SZ"]))
    entry = store.manifest(store.head()).datasets["st_daily"][0]
    path = store.files_dir / entry.path
    pl.read_parquet(path).with_columns(pl.lit("x").alias("extra")).write_parquet(path)
    df = store.read("st_daily")
    assert df.columns == list(datasets_module.ST_DAILY_SCHEMA)
    assert df.get_column("symbol").to_list() == ["000001.SZ"]
