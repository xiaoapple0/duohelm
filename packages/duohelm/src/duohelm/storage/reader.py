"""行情读取：按快照读取证券、日线（含复权）和覆盖统计。

设计见 docs/subsystems/data.md 的“读取接口”一节。要点：

- 只读：不写数据、不产生快照
- 每次调用只解析一次快照 id（默认当前快照），之后的读取都用这个 id
- 数据不存在（快照、数据集、证券）抛 :class:`DataNotFoundError`，不返回空表冒充；
  证券存在但区间内没有日线是正常结果，返回空表并由调用方标明
- 复权只复用 ``duohelm_sdk.adjust``；前复权的基准日不能晚于所读区间，因子也只读到区间结束
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import polars as pl

from duohelm.storage.catalog import SnapshotInfo, SnapshotNotFoundError
from duohelm.storage.store import DataStore, Manifest
from duohelm_sdk.adjust import adjust_bars

AdjustMode = Literal["none", "hfq", "qfq"]
ADJUST_MODES: tuple[AdjustMode, ...] = ("none", "hfq", "qfq")
CALENDAR_MARKET = "CN"

_SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")
_SHANGHAI = timezone(timedelta(hours=8))
"""北京时间，固定偏移（1991 年以后没有夏令时），不依赖 tzdata 和本机时区。"""
_COVERAGE_CACHE_SIZE = 2
_COVERAGE_COLUMNS = (
    "symbol",
    "name",
    "kind",
    "exchange",
    "delist_date",
    "first_date",
    "last_date",
    "bar_count",
    "expected_days",
    "missing_days",
    "off_calendar_bars",
)


class DataNotFoundError(LookupError):
    """快照、数据集或证券不存在。消息是给使用者看的说明。"""


class InvalidQueryError(ValueError):
    """查询参数不合法。"""


class DataReadError(RuntimeError):
    """数据存在但读取失败（文件缺失、损坏）。不能用空结果代替。"""


@dataclass(frozen=True)
class SecuritiesPage:
    snapshot: str
    total: int
    items: pl.DataFrame


@dataclass(frozen=True)
class BarsResult:
    snapshot: str
    symbol: str
    adjust: AdjustMode
    base_date: date | None
    """实际使用的前复权基准日；非前复权、或区间为空且没有传入基准日时为 None。"""
    last_bar_date: date | None
    """这只证券在这个快照里的最后一根日线的日期（不受 start、end 限制）。"""
    bars: pl.DataFrame

    @property
    def empty(self) -> bool:
        return self.bars.height == 0


@dataclass(frozen=True)
class CoveragePage:
    snapshot: str
    total: int
    items: pl.DataFrame


@dataclass(frozen=True)
class CoverageSummary:
    snapshot: str
    as_of: date
    total: int
    groups: pl.DataFrame


class MarketReader:
    def __init__(self, store: DataStore) -> None:
        self._store = store
        # 快照 id -> 每只证券的覆盖统计（扫描 bars_1d 的结果）；快照不可变，不会过期
        self._coverage_cache: dict[str, pl.DataFrame] = {}
        self._coverage_lock = threading.Lock()

    # ---- 快照 ----

    def current_snapshot(self) -> SnapshotInfo:
        head = self._store.head()
        if head is None:
            raise DataNotFoundError("还没有任何快照：请先运行 data.sync 任务")
        return self._store.catalog.snapshot(head)

    def snapshots(self, *, limit: int = 50) -> tuple[str | None, list[SnapshotInfo]]:
        """(当前快照 id, 快照列表)，新的在前，不含已清理的。"""
        return self._store.head(), self._store.catalog.snapshots(limit=limit)

    # ---- 证券 ----

    def securities(
        self,
        *,
        snapshot: str | None = None,
        symbol: str | None = None,
        name_prefix: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> SecuritiesPage:
        _check_page(limit, offset)
        snap, _ = self._open(snapshot, "securities")
        df = self._store.read("securities", snapshot=snap)
        if symbol is not None:
            df = df.filter(pl.col("symbol") == symbol)
        if name_prefix is not None:
            df = df.filter(pl.col("name").str.starts_with(name_prefix))
        if kind is not None:
            df = df.filter(pl.col("kind") == kind)
        return SecuritiesPage(snap, df.height, df.slice(offset, limit))

    # ---- 日线 ----

    def bars(
        self,
        symbol: str,
        *,
        snapshot: str | None = None,
        start: date | None = None,
        end: date | None = None,
        adjust: AdjustMode = "none",
        base_date: date | None = None,
    ) -> BarsResult:
        """某只证券 [start, end]（含两端）的日线，按日期升序。

        前复权的基准日：不传时是返回的日线里最后一天；传入时不能晚于 end（没有 end 时是
        这只证券的最后一根日线），也不能晚于这只证券在快照里的最后一根日线。复权因子只
        读到 end，基准日之后的因子不参与计算。
        """
        if adjust not in ADJUST_MODES:
            raise InvalidQueryError(f"adjust 必须是 {list(ADJUST_MODES)} 之一：{adjust!r}")
        if start is not None and end is not None and start > end:
            raise InvalidQueryError(f"start {start} 晚于 end {end}")
        if base_date is not None and adjust != "qfq":
            raise InvalidQueryError("base_date 只用于前复权（adjust=qfq）")
        needs = ("securities", "bars_1d") + (("adj_factors",) if adjust != "none" else ())
        snap, _ = self._open(snapshot, *needs)
        if self._store.read("securities", snapshot=snap, symbols=[symbol]).height == 0:
            raise DataNotFoundError(f"证券 {symbol} 在快照 {snap} 里不存在")

        history = self._store.read("bars_1d", snapshot=snap, symbols=[symbol])
        last_bar = _max_date(history)
        if base_date is not None:
            if end is not None and base_date > end:
                raise InvalidQueryError(
                    f"前复权基准日 {base_date} 晚于所读区间的结束 {end}：不能用区间之后的复权因子"
                )
            if last_bar is not None and base_date > last_bar:
                raise InvalidQueryError(
                    f"前复权基准日 {base_date} 晚于 {symbol} 在这个快照里"
                    f"最后一个日线日期 {last_bar}"
                )

        rows = history
        if start is not None:
            rows = rows.filter(pl.col("date") >= start)
        if end is not None:
            rows = rows.filter(pl.col("date") <= end)
        base = base_date
        if rows.height and adjust != "none":
            factors = self._store.read("adj_factors", snapshot=snap, symbols=[symbol], end=end)
            if adjust == "qfq" and base is None:
                base = _max_date(rows)
            rows = adjust_bars(rows, factors, mode=adjust, base_date=base)
        return BarsResult(snap, symbol, adjust, base, last_bar, rows)

    # ---- 覆盖统计 ----

    def coverage(
        self,
        *,
        snapshot: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> CoveragePage:
        """每只证券的首末日期、日线根数和按 CN 交易日历缺失的天数。

        列：symbol、name、kind、exchange、delist_date、first_date、last_date、bar_count、
        expected_days、missing_days、off_calendar_bars。没有日线的证券 bar_count 为 0，其余为
        null。停牌日在库里没有行，所以也算缺失。证券列表里没有的代码，name、kind、exchange、
        delist_date 为 null（未知）。证券列表读取失败抛 :class:`DataReadError`。
        """
        _check_page(limit, offset)
        snap, _ = self._open(snapshot, "securities", "bars_1d", "calendar")
        merged = self._coverage_stats(snap)
        if kind is not None:
            merged = merged.filter(pl.col("kind") == kind)
        return CoveragePage(
            snap, merged.height, merged.select(_COVERAGE_COLUMNS).slice(offset, limit)
        )

    def coverage_summary(
        self, *, snapshot: str | None = None, today: date | None = None
    ) -> CoverageSummary:
        """按交易所、种类、在市/退市分组的数量和覆盖统计。

        在市/退市（status）：证券列表里没有退市日或退市日晚于 ``today`` 为 listed，不晚于
        ``today`` 为 delisted；证券列表里没有这个代码时为 null（未知）。``today`` 默认是北京
        日期。交易所、种类为 null 的单独成组。
        """
        snap, _ = self._open(snapshot, "securities", "bars_1d", "calendar")
        as_of = today or datetime.now(_SHANGHAI).date()
        stats = self._coverage_stats(snap)
        status = (
            pl.when(~pl.col("_in_securities"))
            .then(None)
            .when(pl.col("delist_date") <= as_of)
            .then(pl.lit("delisted"))
            .otherwise(pl.lit("listed"))
            .alias("status")
        )
        groups = (
            stats.with_columns(status)
            .group_by("exchange", "kind", "status")
            .agg(
                pl.len().cast(pl.Int64).alias("securities"),
                (pl.col("bar_count") > 0).sum().cast(pl.Int64).alias("with_bars"),
                (pl.col("bar_count") == 0).sum().cast(pl.Int64).alias("no_bars"),
                pl.col("bar_count").sum().cast(pl.Int64),
                (pl.col("missing_days") > 0).sum().cast(pl.Int64).alias("with_missing"),
                pl.col("missing_days").sum().cast(pl.Int64),
                pl.col("off_calendar_bars").sum().cast(pl.Int64),
            )
            .sort("exchange", "kind", "status", nulls_last=True)
        )
        return CoverageSummary(snap, as_of, stats.height, groups)

    def _coverage_stats(self, snap: str) -> pl.DataFrame:
        """快照里每只证券的覆盖统计，按代码排序。按快照 id 缓存，同一快照只扫描一次 bars_1d。"""
        with self._coverage_lock:  # 并发的第一批请求排队等同一次扫描
            cached = self._coverage_cache.pop(snap, None)
            if cached is None:
                cached = self._scan_coverage(snap)
            self._coverage_cache[snap] = cached  # 重新插入：最近用到的排在最后
            while len(self._coverage_cache) > _COVERAGE_CACHE_SIZE:
                del self._coverage_cache[next(iter(self._coverage_cache))]
            return cached

    def _scan_coverage(self, snap: str) -> pl.DataFrame:
        calendar = (
            self._store.read("calendar", snapshot=snap, columns=["market", "date"])
            .filter(pl.col("market") == CALENDAR_MARKET)
            .get_column("date")
            .sort()
        )
        if calendar.len() == 0:
            raise DataNotFoundError(f"快照 {snap} 里没有 {CALENDAR_MARKET} 交易日历")
        try:
            securities = self._store.read(
                "securities",
                snapshot=snap,
                columns=["symbol", "name", "kind", "exchange", "delist_date"],
            )
        except Exception as e:
            # 读取失败是“未知”，不能当成交易所、退市日为空而继续
            raise DataReadError(f"读取快照 {snap} 的证券列表失败：{e}") from e
        securities = securities.with_columns(pl.lit(True).alias("_in_securities"))
        bars = self._store.read("bars_1d", snapshot=snap, columns=["symbol", "date"])

        stats = bars.group_by("symbol").agg(
            pl.col("date").min().alias("first_date"),
            pl.col("date").max().alias("last_date"),
            pl.len().cast(pl.Int64).alias("bar_count"),
            pl.col("date").is_in(calendar.implode()).sum().cast(pl.Int64).alias("_on_calendar"),
        )
        # 首末日期之间（含两端）的交易日数：日历里 <= last 的个数减去 < first 的个数
        stats = stats.with_columns(
            (
                calendar.search_sorted(stats.get_column("last_date"), side="right")
                - calendar.search_sorted(stats.get_column("first_date"), side="left")
            )
            .cast(pl.Int64)
            .alias("expected_days")
        ).with_columns(
            (pl.col("expected_days") - pl.col("_on_calendar")).alias("missing_days"),
            (pl.col("bar_count") - pl.col("_on_calendar")).alias("off_calendar_bars"),
        )
        return (
            securities.join(stats, on="symbol", how="full", coalesce=True)
            .with_columns(
                pl.col("bar_count").fill_null(0),
                pl.col("_in_securities").fill_null(False),
            )
            .select(*_COVERAGE_COLUMNS, "_in_securities")
            .sort("symbol")
        )

    # ---- 每日快照：ST 名单和指数成分（#54）----

    def st_on(self, day: date, *, snapshot: str | None = None) -> pl.DataFrame:
        """day 这个交易日能用的 ST 名单（date、symbol、name、status、observed_at）。

        只取 observed_at 之后的下一个交易日恰好是 day 的快照，不能用拍摄当天的；不向前顺延。
        没有这样的快照抛 DataNotFoundError（“不知道”，不是“没有 ST”）。
        """
        return self._daily("st_daily", day, snapshot, None)

    def index_members_on(
        self, index: str, day: date, *, snapshot: str | None = None
    ) -> pl.DataFrame:
        """day 这个交易日能用的指数 index 成分（含 observed_at），规则同 st_on。"""
        return self._daily("index_members_daily", day, snapshot, index)

    def _daily(
        self, dataset: str, day: date, snapshot: str | None, index: str | None
    ) -> pl.DataFrame:
        snap, _ = self._open(snapshot, "calendar", dataset)
        calendar = (
            self._store.read("calendar", snapshot=snap, columns=["market", "date"])
            .filter(pl.col("market") == CALENDAR_MARKET)
            .get_column("date")
        )
        prev = calendar.filter(calendar < day).max()
        if day not in calendar.to_list() or not isinstance(prev, date):
            raise DataNotFoundError(
                f"{day} 不在快照 {snap} 的 {CALENDAR_MARKET} 日历里，或没有前一个交易日"
            )
        # 同步保证拍摄日期就是快照日期；多往前读几天，是给手工写入的、跨日的行留余地
        rows = self._store.read(
            dataset, snapshot=snap, start=prev - timedelta(days=7), end=day - timedelta(days=1)
        )
        if index is not None:
            rows = rows.filter(pl.col("index") == index)
        # 没有 observed_at 的旧行，拍摄日期按快照日期算（见 data.md）
        taken = pl.coalesce(
            pl.col("observed_at").dt.convert_time_zone("Asia/Shanghai").dt.date(), pl.col("date")
        )
        rows = rows.filter((taken >= prev) & (taken < day))
        latest = rows.get_column("date").max()
        if not isinstance(latest, date):
            what = f"{dataset}{'' if index is None else f' 的 {index}'}"
            raise DataNotFoundError(
                f"快照 {snap} 里没有可用于 {day} 的 {what}：只能用 {prev} 及之后、{day} 之前拍的"
            )
        return rows.filter(pl.col("date") == latest)

    # ---- 内部 ----

    def _open(self, snapshot: str | None, *datasets: str) -> tuple[str, Manifest]:
        """解析快照 id 并确认需要的数据集都存在。"""
        if snapshot is None:
            snap = self._store.head()
            if snap is None:
                raise DataNotFoundError("还没有任何快照：请先运行 data.sync 任务")
        else:
            if not _SNAPSHOT_ID.fullmatch(snapshot):
                raise InvalidQueryError("快照 id 必须是 64 位小写十六进制")
            snap = snapshot
        try:
            manifest = self._store.manifest(snap)
        except SnapshotNotFoundError:
            raise DataNotFoundError(f"快照 {snap} 不存在或已被清理") from None
        missing = _missing(manifest, datasets)
        if missing:
            raise DataNotFoundError(f"快照 {snap} 里没有数据集：{', '.join(missing)}")
        return snap, manifest


def _missing(manifest: Manifest, datasets: Iterable[str]) -> list[str]:
    return [d for d in datasets if not manifest.datasets.get(d)]


def _max_date(df: pl.DataFrame) -> date | None:
    value = df.get_column("date").max()
    return value if isinstance(value, date) else None


def _check_page(limit: int, offset: int) -> None:
    if limit < 1 or offset < 0:
        raise InvalidQueryError("limit 必须为正，offset 不能为负")
