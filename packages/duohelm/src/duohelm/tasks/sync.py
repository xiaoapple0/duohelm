"""data.sync：从行情源插件同步证券列表、交易日历、日线和复权因子，提交为一个快照。

设计见 docs/subsystems/data.md 的“同步任务”一节。要点：

- 插件经 :func:`duohelm.isolation.session_for` 在隔离进程里运行，带上用户在插件页确认过的
  连接地址（#24）。没确认的 endpoint，插件拿不到地址
- 开始时先调用插件的 ``check_connection``（如果实现了）；没通过就直接失败，不进入下载
- 证券列表必须包含退市证券，否则回测有幸存者偏差。``lists_delisted`` 为 False 的数据源
  先按代码区间扫描退市股（:mod:`duohelm.tasks.delisted`，#30），扫完才继续
- 入库前按 SDK 的契约检查格式；成交量为 0 的行（停牌占位）丢弃
- 只同步已收盘的日子：盘中的日线不是最终值，不能进快照
- 每天保存 ST 名单和指数成分的快照（:mod:`duohelm.tasks.snapshots`，#43）：只在同步到最近一个
  已收盘的日子时写，回补历史时不写
- 全部数据在一个事务里写入，成功才提交为一个快照；失败、取消时不留下半个快照。重复执行
  只写入有变化的行，同样的数据不会产生新快照
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Self

import polars as pl
from pydantic import BaseModel, Field, field_validator, model_validator

from duohelm.endpoints import EndpointStore
from duohelm.isolation.runner import Limits, PluginRunError, PluginSession, session_for
from duohelm.registry import PluginEntry, PluginRegistry, PluginStatus
from duohelm.settings import Settings
from duohelm.storage import DATASETS, DataStore, SnapshotConflictError
from duohelm.storage.store import Transaction
from duohelm.tasks.delisted import discover_securities
from duohelm.tasks.snapshots import (
    DEFAULT_INDEXES,
    SnapshotUnavailableError,
    fetch_index_members,
    st_snapshot,
    stale_keys,
)
from duohelm.tasks.types import (
    RetryableError,
    TaskCancelled,
    TaskContext,
    TaskLost,
    TaskParams,
    TaskType,
)
from duohelm_sdk import Capabilities, ConnectionStatus, Freq
from duohelm_sdk.checks import check_adj_factors, check_securities
from duohelm_sdk.schema import BARS_SCHEMA, known_securities, schema_problems

MARKET = "CN"
EARLIEST = date(1990, 12, 19)
"""上交所开市日。默认从这一天开始全量同步，不设起始年份。"""
BATCH_SIZE = 50
"""每次向插件请求多少只证券的日线和复权因子。"""
SHANGHAI = timezone(timedelta(hours=8))
"""北京时间。1991 年以后没有夏令时，用固定偏移，平台不依赖 tzdata。"""
CLOSE = time(15, 30)
"""北京时间这个时刻之后，当天的日线才算最终值。A 股 15:00 收盘，科创板、创业板的盘后
固定价格交易到 15:30，成交量和成交额要到那时才定下来。"""
_RETRYABLE_REASONS = frozenset({"timeout", "crashed"})
_CANCEL_POLL_S = 1.0
_MAX_EXAMPLES = 5
_SNAPSHOT_LOOKBACK = timedelta(days=15)
"""找快照日期（最近一个交易日）时往前看多少天的日历。春节、国庆最长休市 9 天左右。"""


class SyncError(Exception):
    """同步不能进行，或插件返回的数据不合格。不自动重试。"""


class SyncParams(TaskParams):
    plugin_id: str = Field(min_length=1)
    """行情源插件的 id。"""
    start: date = EARLIEST
    end: date | None = None
    """None 表示执行时最近一个已收盘的日子（见 :func:`last_closed_day`）。"""
    indexes: list[str] | None = None
    """保存成分快照的指数代码；None 表示默认的一组宽基指数，空列表表示不保存。"""

    @field_validator("indexes")
    @classmethod
    def _check_indexes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [i.strip() for i in value]
        if any(not i for i in cleaned):
            raise ValueError("indexes 里有空的指数代码")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"end（{self.end}）不能早于 start（{self.start}）")
        return self


class SyncResult(BaseModel):
    plugin_id: str
    start: date
    end: date
    snapshot_id: str | None
    """新快照的 id；数据没有任何变化时为 None。"""
    connection: str | None
    """check_connection 的说明；插件没有实现时为 None。"""
    securities: int
    """写入的证券数（含退市）。"""
    symbols: int
    """请求了日线和复权因子的证券数。"""
    written: dict[str, int]
    """各数据集实际写入（新增或变化）的行数。"""
    dropped_zero_volume: int
    """丢弃的成交量为 0 的行数。"""
    scanned: int = 0
    """退市股扫描：这次新扫描的代码数。能直接列出退市证券的数据源为 0。"""
    rechecked: int = 0
    """退市股扫描：这次重新查询的、扫到过但不在代码表里的代码数。"""
    snapshot_date: date | None = None
    """ST 名单和指数成分快照的日期；这次没有写快照时为 None。"""
    snapshot_skipped: str | None = None
    """没有写快照的原因（同步区间早于最近一个已收盘的日子等）；写了为 None。"""
    st_snapshot_failed: str | None = None
    """取得了快照日期，但 ST 名单没写的原因（如名称全部为空）。"""
    st_unknown_names: int = 0
    """名称为空、ST 状态未知而没有写入的在市股票数。"""
    index_snapshot_failed: dict[str, str] = Field(default_factory=dict)
    """没有写成分快照的指数及原因（插件没实现、报错、格式不对、成分为空）。"""


def _beijing_now() -> datetime:
    return datetime.now(SHANGHAI)


def last_closed_day(now: Callable[[], datetime] = _beijing_now) -> date:
    """最近一个已收盘的日子：北京时间 15:30 之后是今天，之前是昨天。

    不判断是不是交易日：非交易日插件不返回日线，多请求一天没有影响。
    """
    current = now().astimezone(SHANGHAI)
    today = current.date()
    return today if current.time() >= CLOSE else today - timedelta(days=1)


def data_sync(
    settings: Settings,
    *,
    limits: Limits | None = None,
    now: Callable[[], datetime] = _beijing_now,
) -> TaskType:
    def handler(ctx: TaskContext, params: SyncParams) -> SyncResult:
        closed = last_closed_day(now)
        end = params.end or closed
        if end > closed:
            raise SyncError(
                f"end（{end}）还没有收盘：盘中的日线不是最终值，不能进快照。"
                f"最近一个已收盘的日子是 {closed}（北京时间 {CLOSE:%H:%M} 之后算当天收盘）"
            )
        if end < params.start:
            raise SyncError(f"start（{params.start}）晚于最近一个已收盘的日子（{end}）")
        entry, deps = find_datasource(settings, params.plugin_id)
        endpoints = EndpointStore(settings.plugin_config_path)
        assert entry.manifest is not None  # find_datasource 已检查
        addresses = endpoints.addresses_for(entry.manifest)
        data = DataStore(settings.data_dir)
        run_limits = limits or Limits.from_env()

        def plugin_log(line: str) -> None:
            ctx.log(f"[{entry.id}] {line}")

        with (
            _cancel_watch(ctx) as cancel,
            _plugin_errors(ctx, entry.id),
            session_for(
                entry, endpoints, deps=deps, limits=run_limits, cancel=cancel, log=plugin_log
            ) as session,
        ):
            return _run(
                ctx,
                session,
                data,
                params.start,
                end,
                addresses=addresses,
                closed=closed,
                indexes=DEFAULT_INDEXES if params.indexes is None else tuple(params.indexes),
            )

    return TaskType(
        name="data.sync",
        params_model=SyncParams,
        handler=handler,
        description="从行情源插件同步证券列表、交易日历、日线和复权因子，提交为一个快照",
    )


def find_datasource(settings: Settings, plugin_id: str) -> tuple[PluginEntry, list[PluginEntry]]:
    """按 id 找到可以运行的行情源插件及其依赖。"""
    entries = PluginRegistry([settings.plugin_dir]).scan()
    by_id = {e.id: e for e in entries}
    entry = by_id.get(plugin_id)
    if entry is None:
        raise SyncError(f"找不到插件 {plugin_id}（插件目录 {settings.plugin_dir}）")
    if entry.status is not PluginStatus.READY or entry.manifest is None:
        problems = "；".join(entry.problems) or "无"
        raise SyncError(f"插件 {plugin_id} 的状态是 {entry.status}，不能运行。问题：{problems}")
    if entry.manifest.plugin.kind != "datasource":
        raise SyncError(f"插件 {plugin_id} 不是行情源（kind 为 {entry.manifest.plugin.kind}）")
    deps = [by_id[r.id] for r in entry.manifest.requires if r.id in by_id]
    return entry, deps


# ---------------------------------------------------------------- 主流程


def _run(
    ctx: TaskContext,
    session: PluginSession,
    data: DataStore,
    start: date,
    end: date,
    *,
    addresses: Mapping[str, str],
    closed: date,
    indexes: Sequence[str],
) -> SyncResult:
    plugin_id = session.entry.id
    ctx.log(f"同步 {plugin_id}：{start} 至 {end}")
    ctx.progress(0.0, "检查连接")
    connection = check_connection(session, addresses)
    ctx.log(connection if connection is not None else "插件没有实现 check_connection，跳过")
    caps = check_capabilities(plugin_id, session.call("capabilities"))

    ctx.progress(0.01, "证券列表")
    scanned = rechecked = 0
    if caps.lists_delisted:
        securities = fetch_securities(session)
        base = 0.02
    else:
        scan = discover_securities(
            ctx, session, data.catalog, progress=lambda f, m: ctx.progress(0.01 + 0.29 * f, m)
        )
        securities = known_securities(scan.securities).sort("symbol")
        scanned, rechecked = scan.scanned, scan.rechecked
        base = 0.3
    ctx.progress(base, "交易日历")
    calendar = fetch_calendar(session, start, end)
    symbols = symbols_in_range(securities, start, end)
    ctx.log(
        f"证券 {securities.height} 只，其中 {len(symbols)} 只在同步区间内；"
        f"交易日 {calendar.height} 天"
    )

    written = {
        "securities": 0,
        "delist_date_quality": 0,
        "calendar": 0,
        "bars_1d": 0,
        "adj_factors": 0,
        "st_daily": 0,
        "index_members_daily": 0,
    }
    snap = _prepare_snapshots(
        ctx, session, securities=securities, end=end, closed=closed, indexes=indexes
    )
    if not caps.exact_delist_date:
        ctx.log(f"插件 {plugin_id} 的退市日是近似值，记进 delist_date_quality")
    dropped = 0
    batches = [symbols[i : i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    try:
        with data.transaction(
            task_id=ctx.task_id, note=f"data.sync {plugin_id} {start}..{end}"
        ) as tx:
            written["securities"] = tx.write("securities", securities)
            written["delist_date_quality"] = tx.write(
                "delist_date_quality", delist_date_quality(securities, caps)
            )
            written["calendar"] = tx.write("calendar", calendar)
            if snap.day is not None:
                if snap.st is not None:
                    written["st_daily"] = _write_day(tx, data, "st_daily", snap.day, snap.st)
                for index, members in snap.members.items():
                    written["index_members_daily"] += _write_day(
                        tx, data, "index_members_daily", snap.day, members, index=index
                    )
            for i, batch in enumerate(batches):
                ctx.progress(
                    base + 0.01 + (0.98 - base) * i / len(batches),
                    f"日线和复权因子：{i * BATCH_SIZE}/{len(symbols)}",
                )
                bars, zero = fetch_bars(session, batch, start, end)
                dropped += zero
                written["bars_1d"] += tx.write("bars_1d", bars)
                written["adj_factors"] += tx.write(
                    "adj_factors", fetch_adj_factors(session, batch, end)
                )
            ctx.check_cancelled()  # 提交前最后一次检查：取消的任务不留下快照
    except SnapshotConflictError as exc:
        raise RetryableError(f"提交时当前快照已被别的写入改变：{exc}") from exc
    if dropped:
        ctx.log(f"丢弃成交量为 0 的行 {dropped} 行（停牌占位）")
    ctx.log(f"写入：{written}；快照 {tx.snapshot_id or '没有变化，未产生新快照'}")
    ctx.progress(1.0, "完成")
    return SyncResult(
        plugin_id=plugin_id,
        start=start,
        end=end,
        snapshot_id=tx.snapshot_id,
        connection=connection,
        securities=securities.height,
        symbols=len(symbols),
        written=written,
        dropped_zero_volume=dropped,
        scanned=scanned,
        rechecked=rechecked,
        snapshot_date=snap.day,
        snapshot_skipped=snap.skipped,
        st_snapshot_failed=snap.st_failed,
        st_unknown_names=snap.st_unknown,
        index_snapshot_failed=snap.index_failed,
    )


# ---------------------------------------------------------------- ST 名单和指数成分快照


@dataclass
class _Snapshots:
    day: date | None = None
    skipped: str | None = None
    st: pl.DataFrame | None = None
    st_unknown: int = 0
    st_failed: str | None = None
    members: dict[str, pl.DataFrame] = field(default_factory=lambda: {})
    index_failed: dict[str, str] = field(default_factory=lambda: {})


def _prepare_snapshots(
    ctx: TaskContext,
    session: PluginSession,
    *,
    securities: pl.DataFrame,
    end: date,
    closed: date,
    indexes: Sequence[str],
) -> _Snapshots:
    """决定要不要写快照，并取好要写的内容（还没写）。

    快照日期是最近一个已收盘的交易日；同步到的 end 早于最近已收盘的日子时不写：
    名单是现在的名单，不能当成过去的名单。
    """
    if end < closed:
        reason = (
            f"同步区间的 end（{end}）早于最近一个已收盘的日子（{closed}），"
            "现在的名单不能当成过去的名单"
        )
        ctx.log(f"不写 ST 名单和指数成分快照：{reason}")
        return _Snapshots(skipped=reason)
    calendar = fetch_calendar(session, closed - _SNAPSHOT_LOOKBACK, closed)
    if not calendar.height:
        reason = f"{closed - _SNAPSHOT_LOOKBACK} 至 {closed} 的交易日历为空，找不到快照日期"
        ctx.log(f"不写 ST 名单和指数成分快照：{reason}", "warning")
        return _Snapshots(skipped=reason)
    day: date = calendar.get_column("date").max()  # pyright: ignore[reportAssignmentType]
    snap = _Snapshots(day=day)
    try:
        snap.st, snap.st_unknown = st_snapshot(securities, day)
    except SnapshotUnavailableError as exc:
        snap.st_failed = str(exc)
        ctx.log(f"不写 {day} 的 ST 名单：{exc}", "warning")
    else:
        ctx.log(f"{day} 的 ST 名单：{snap.st.height} 只")
        if snap.st_unknown:
            ctx.log(f"{snap.st_unknown} 只在市股票名称为空，ST 状态未知，没有写入", "warning")
    for index in indexes:
        try:
            snap.members[index] = fetch_index_members(session, index, day)
        except SnapshotUnavailableError as exc:
            snap.index_failed[index] = str(exc)
            ctx.log(f"不写 {day} 的 {index} 成分：{exc}", "warning")
        else:
            ctx.log(f"{day} 的 {index} 成分：{snap.members[index].height} 只")
    return snap


def _write_day(
    tx: Transaction,
    data: DataStore,
    dataset: str,
    day: date,
    rows: pl.DataFrame,
    *,
    index: str | None = None,
) -> int:
    """写入 day 的快照。这一天已有的行以这次为准：这次名单里没有的行作废。"""
    existing = data.read(dataset, start=day, end=day)
    if index is not None:
        existing = existing.filter(pl.col("index") == index)
    stale = stale_keys(existing, rows, list(DATASETS[dataset].keys))
    if stale.height:
        tx.delete(dataset, stale)
    return tx.write(dataset, rows)


# ---------------------------------------------------------------- 各步骤


def check_connection(session: PluginSession, addresses: Mapping[str, str]) -> str | None:
    """调用插件的 check_connection。没通过时抛 SyncError；插件没有实现时返回 None。

    插件给的说明会进任务日志和结果，其中用户确认的地址替换成 ``<endpoint id 的地址>``。
    """
    try:
        status = session.call("check_connection")
    except PluginRunError as exc:
        if exc.reason == "unsupported":
            return None
        raise
    if not isinstance(status, ConnectionStatus):
        raise SyncError(f"check_connection 应返回 ConnectionStatus，实际为 {type(status).__name__}")
    message = redact(status.message, addresses)
    if not status.ok:
        raise SyncError(f"连接检查没有通过：{message or '插件没有给出原因'}")
    return message


def redact(message: str, addresses: Mapping[str, str]) -> str:
    """把用户确认的地址换成占位符：任务日志里不写具体地址。"""
    for endpoint_id, address in sorted(addresses.items(), key=lambda kv: -len(kv[1])):
        message = message.replace(address, f"<{endpoint_id} 的地址>")
    return message


def delist_date_quality(securities: pl.DataFrame, caps: Capabilities) -> pl.DataFrame:
    """每只证券一行：有退市日的按数据源的声明记精确或近似，仍在市的为 null。"""
    return securities.select(
        "symbol",
        exact=pl.when(pl.col("delist_date").is_not_null())
        .then(pl.lit(caps.exact_delist_date))
        .otherwise(pl.lit(None, dtype=pl.Boolean())),
    )


def check_capabilities(plugin_id: str, caps: Any) -> Capabilities:
    if not isinstance(caps, Capabilities):
        raise SyncError(f"capabilities 应返回 Capabilities，实际为 {type(caps).__name__}")
    if MARKET not in caps.markets:
        raise SyncError(f"插件 {plugin_id} 不支持 {MARKET} 市场（支持 {list(caps.markets)}）")
    if Freq.DAY not in caps.freqs:
        raise SyncError(f"插件 {plugin_id} 不提供日线")
    return caps


def fetch_securities(session: PluginSession) -> pl.DataFrame:
    """完整的证券列表（含退市），只保留认识的 exchange 和 kind。"""
    df = _frame(session.call("securities", include_delisted=True), "securities")
    result = check_securities(df)
    if not result.passed:
        raise SyncError(f"证券列表不合格：{'；'.join(result.details)}")
    return known_securities(df).sort("symbol")


def fetch_calendar(session: PluginSession, start: date, end: date) -> pl.DataFrame:
    df = _frame(session.call("trading_calendar", MARKET, start, end), "trading_calendar")
    if "date" not in df.columns or df.schema["date"] != pl.Date():
        raise SyncError(f"交易日历应有 Date 类型的 date 列，实际为 {df.schema}")
    dates = df.get_column("date")
    if dates.null_count():
        raise SyncError(f"交易日历有 {dates.null_count()} 个空日期")
    _require_in_range("交易日历", df, start, end)
    return df.select(pl.lit(MARKET).alias("market"), pl.col("date")).unique().sort("date")


def symbols_in_range(securities: pl.DataFrame, start: date, end: date) -> list[str]:
    """在 [start, end] 期间可能有行情的证券：上市日不晚于 end，退市日不早于 start。"""
    listed = pl.col("list_date").is_null() | (pl.col("list_date") <= end)
    alive = pl.col("delist_date").is_null() | (pl.col("delist_date") >= start)
    return securities.filter(listed & alive).get_column("symbol").sort().to_list()


def fetch_bars(
    session: PluginSession, symbols: Sequence[str], start: date, end: date
) -> tuple[pl.DataFrame, int]:
    """未复权日线。返回 (成交量不为 0 的行, 丢弃的行数)。"""
    df = _frame(session.call("bars", list(symbols), Freq.DAY, start, end), "bars")
    problems = schema_problems(df, BARS_SCHEMA)
    if problems:
        raise SyncError(f"日线不合格：{'；'.join(problems)}")
    _require_symbols("日线", df, symbols)
    _require_in_range("日线", df, start, end)
    unknown = df.get_column("volume").null_count()
    if unknown:
        # 成交量不知道不等于 0：当成停牌丢掉会把数据源的缺陷藏起来
        raise SyncError(f"日线有 {unknown} 行成交量为空")
    kept = df.filter(pl.col("volume") != 0.0)
    return kept, df.height - kept.height


def fetch_adj_factors(session: PluginSession, symbols: Sequence[str], end: date) -> pl.DataFrame:
    """复权因子总是从 EARLIEST 请求，不管同步区间从哪天开始。

    因子是累计值，第一行之前视为 1.0。只请求 [start, end] 时，start 之前最后一次除权的
    因子拿不到，区间开头的 K 线会按 1.0 复权；插件若只用区间内的事件计算，累计值还会从
    1 重新开始，和已入库的因子基数不一致。因子行很少，全量请求的代价可以忽略。
    """
    df = _frame(session.call("adj_factors", list(symbols), EARLIEST, end), "adj_factors")
    result = check_adj_factors(df)
    if not result.passed:
        raise SyncError(f"复权因子不合格：{'；'.join(result.details)}")
    _require_symbols("复权因子", df, symbols)
    _require_in_range("复权因子", df, EARLIEST, end)
    return df


# ---------------------------------------------------------------- 辅助


def _frame(value: object, method: str) -> pl.DataFrame:
    if not isinstance(value, pl.DataFrame):
        raise SyncError(f"{method} 应返回 DataFrame，实际为 {type(value).__name__}")
    return value


def _require_symbols(what: str, df: pl.DataFrame, symbols: Sequence[str]) -> None:
    extra = df.filter(~pl.col("symbol").is_in(list(symbols))).get_column("symbol").unique()
    if extra.len():
        raise SyncError(
            f"{what}里有没有请求的证券：{extra.len()} 只，"
            f"例如 {extra.sort().head(_MAX_EXAMPLES).to_list()}"
        )


def _require_in_range(what: str, df: pl.DataFrame, start: date, end: date) -> None:
    outside = df.filter(~pl.col("date").is_between(start, end))
    if outside.height:
        examples = outside.get_column("date").sort().head(_MAX_EXAMPLES).to_list()
        raise SyncError(f"{what}有 {outside.height} 行不在 {start} 至 {end} 之间，例如 {examples}")


@contextmanager
def _cancel_watch(ctx: TaskContext) -> Generator[threading.Event]:
    """后台线程定期检查任务是否被取消（或租约被接管），是则设置 cancel，让会话立即结束子进程。

    插件的一次调用可能很长（下载一批日线），只在调用之间检查取消不够及时。
    """
    cancel = threading.Event()
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(_CANCEL_POLL_S):
            try:
                ctx.check_cancelled()
            except (TaskCancelled, TaskLost):
                cancel.set()
                return
            except Exception:  # 数据库暂时不可用时下一轮再查
                continue

    thread = threading.Thread(target=watch, name=f"sync-cancel-{ctx.task_id}", daemon=True)
    thread.start()
    try:
        yield cancel
    finally:
        stop.set()
        thread.join()


@contextmanager
def _plugin_errors(ctx: TaskContext, plugin_id: str) -> Generator[None]:
    """把插件运行错误转成任务系统认识的结果：取消、可重试、不可重试。"""
    try:
        yield
    except PluginRunError as exc:
        if exc.reason == "cancelled":
            ctx.check_cancelled()  # 抛出 TaskCancelled 或 TaskLost
            raise SyncError(f"插件 {plugin_id} 被意外结束：{exc.message}") from exc
        if exc.traceback:
            ctx.log(exc.traceback, "error")
        message = f"插件 {plugin_id} 出错（{exc.reason}）：{exc.message}"
        if exc.reason in _RETRYABLE_REASONS:
            raise RetryableError(message) from exc
        raise SyncError(message) from exc


__all__ = [
    "BATCH_SIZE",
    "EARLIEST",
    "MARKET",
    "SyncError",
    "SyncParams",
    "SyncResult",
    "data_sync",
    "find_datasource",
    "last_closed_day",
]
