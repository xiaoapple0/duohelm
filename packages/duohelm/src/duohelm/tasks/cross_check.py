"""data.cross_check：两个行情源插件的交叉核对（#32）。只读。

对同一区间、同一批证券，比较两个数据源的未复权日线（OHLCV）和复权因子，列出差异超过阈值的
证券和日期，另外列出只在一边出现的日期。用来发现单个数据源的错误：漏数据、复权错、日期错位。
设计见 docs/subsystems/data.md 的“交叉核对”一节。要点：

- 不写入任何数据集，不产生快照；结果只放在任务结果里。差异样例按类别设上限，总数另记
- 成交量按 SDK 契约比较：契约规定是股，不做换算。某个证券两边的成交量几乎恒为 100 倍关系，
  说明有一边的单位不是股，单独报告为 ``volume_unit_mismatch``，不当成逐日的成交量差异
- 读取失败是“未知”，不是“没有差异”：某个数据源整体不可用，或某一批证券读取、校验失败，
  相关证券记进 ``unknown_symbols``，``complete`` 为 False
- 成交量为 0 的行（停牌占位）不参与比较，同 ``data.sync`` 入库时丢弃它们；成交量等字段为空（或 NaN）
  时 ``data.sync`` 直接判为不合格；这里空字段不比较、只计数，并让结果不完整（``partial``）
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Self

import polars as pl
from pydantic import BaseModel, Field, field_validator, model_validator

from duohelm.endpoints import EndpointStore
from duohelm.isolation.runner import Limits, PluginRunError, PluginSession, session_for
from duohelm.settings import Settings
from duohelm.tasks.sync import (
    EARLIEST,
    SyncError,
    _cancel_watch,  # pyright: ignore[reportPrivateUsage]
    check_capabilities,
    check_connection,
    find_datasource,
    last_closed_day,
    redact,
    symbols_in_range,
)
from duohelm.tasks.types import TaskContext, TaskParams, TaskType
from duohelm_sdk import Capabilities, Freq
from duohelm_sdk.checks import check_adj_factors, check_securities
from duohelm_sdk.schema import BARS_SCHEMA, known_securities, schema_problems

BATCH_SIZE = 50
"""每次向每个插件请求多少只证券。"""
PRICE_FIELDS = ("open", "high", "low", "close")
UNIT_RATIOS = (100.0, 0.01)
"""成交量单位疑似不同的比值：手和股。"""
UNIT_RATIO_TOLERANCE = 0.02
UNIT_MIN_ROWS = 5
UNIT_MIN_SHARE = 0.9
"""一个证券至少有这么多行两边成交量都大于 0，且这些行里至少这个比例的 a/b 都接近 100（或 0.01），
才判为单位不一致。"""
_MAX_REASON = 300

Kind = Literal[
    "bar_mismatch",
    "only_in_a",
    "only_in_b",
    "volume_unit_mismatch",
    "factor_mismatch",
]


Day = date
"""Difference 里有叫 date 的字段，类体内的注解要用别名，否则 pydantic 会解析到字段默认值。"""


class CrossCheckError(Exception):
    """交叉核对不能进行（参数不对、插件不能运行）。不自动重试。"""


class CrossCheckParams(TaskParams):
    plugin_a: str = Field(min_length=1)
    """第一个行情源插件的 id。结果里的 a 指它。"""
    plugin_b: str = Field(min_length=1)
    symbols: list[str] | None = None
    """要核对的证券。None 表示两个数据源的证券列表的交集（在 [start, end] 内可能有行情的证券）。
    股票池功能（M2）还没有，之后可以由股票池展开成这个列表。"""
    start: date
    end: date | None = None
    """None 表示执行时最近一个已收盘的日子（同 data.sync）。"""
    price_tolerance: float = Field(default=1e-3, ge=0, le=1, allow_inf_nan=False)
    """开高低收的相对差异阈值：|a-b| / max(|a|,|b|) 超过它才算差异。"""
    volume_tolerance: float = Field(default=1e-2, ge=0, le=1, allow_inf_nan=False)
    """成交量的相对差异阈值（单位都是股）。"""
    factor_tolerance: float = Field(default=1e-3, ge=0, le=1, allow_inf_nan=False)
    """复权因子的相对差异阈值。"""
    max_examples: int = Field(default=100, ge=0, le=10_000)
    """每一类差异最多保留多少条样例；总数不受限制，另记在 ``counts`` 里。"""

    @field_validator("symbols")
    @classmethod
    def _check_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [s.strip() for s in value]
        if any(not s for s in cleaned):
            raise ValueError("symbols 里有空的证券代码")
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            raise ValueError("symbols 不能为空列表；要用两个数据源的交集请不传")
        return unique

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.plugin_a == self.plugin_b:
            raise ValueError("plugin_a 和 plugin_b 不能是同一个插件")
        if self.end is not None and self.end < self.start:
            raise ValueError(f"end（{self.end}）不能早于 start（{self.start}）")
        return self


class Difference(BaseModel):
    kind: Kind
    symbol: str
    date: Day | None = None
    """日期；``volume_unit_mismatch`` 是整个证券的差异，为 None。"""
    field: str | None = None
    """``bar_mismatch`` 的字段（open、high、low、close、volume）；``factor_mismatch`` 为 factor。"""
    a: float | None = None
    b: float | None = None
    rel_diff: float | None = None
    """相对差异 |a-b| / max(|a|,|b|)。"""
    note: str | None = None


class SourceStatus(BaseModel):
    plugin_id: str
    ok: bool
    """False 表示这个数据源整体不可用（连接、能力检查、证券列表失败）；批次级的失败见
    ``unknown_symbols``。"""
    error: str | None = None
    batch_errors: int = 0
    """读取或校验失败的批次数。"""


class UnknownSymbols(BaseModel):
    symbol: str
    unknown_in: list[str]
    """哪些数据源（a、b）这只证券的数据读取失败。"""
    reason: str


class CrossCheckResult(BaseModel):
    plugin_a: str
    plugin_b: str
    start: date
    end: date
    status: Literal["compared", "partial", "unknown"]
    """compared：所有证券两边都读到并比较了，且没有空字段；partial：有证券的数据读取失败（未知），
    或有字段为空没有比较（``unknown_fields`` 不为空），其余比较了；unknown：一个证券也没比较成。
    无论哪种，只要有差异，都在 ``counts`` 里。"""
    complete: bool
    """status 为 compared。False 时不能当成“没有差异”。"""
    sources: dict[str, SourceStatus]
    """键是 ``a``、``b``。"""
    symbols_requested: int
    symbols_compared: int
    unknown_symbols_total: int
    unknown_symbols: list[UnknownSymbols]
    """读取失败、没有比较的证券，最多 ``max_examples`` 条。"""
    only_listed_in_a: list[str] = Field(default_factory=lambda: [])
    only_listed_in_b: list[str] = Field(default_factory=lambda: [])
    """没有传 symbols 时：只在一边的证券列表里出现的代码数量见下面的 ``*_total``，这里是样例。
    不能直接当成漏数据：``lists_delisted`` 为 False 的数据源列表里没有退市证券。"""
    only_listed_in_a_total: int = 0
    only_listed_in_b_total: int = 0
    bars_compared: int = 0
    """两边都有的 (证券, 日期) 行数。"""
    bars_consistent: int = 0
    """其中一致的行数：开高低收成交量两边都不为空（NaN 按空处理）、都在阈值内，且证券没有成交量
    单位差异。有空字段的行不算一致，也不算差异，见 ``unknown_fields``。"""
    unknown_fields: dict[str, int] = Field(default_factory=lambda: {})
    """两边都有的行里，某个字段至少有一边为空、没有比较的次数，按字段。"""
    counts: dict[str, int] = Field(default_factory=lambda: {})
    """每一类差异的总数，按 kind；``bar_mismatch`` 另按字段拆分，键为 ``bar_mismatch.close`` 等。"""
    symbols_with_differences: int = 0
    top_symbols: dict[str, int] = Field(default_factory=lambda: {})
    """差异最多的证券（最多 ``max_examples`` 个）及差异条数。"""
    examples: list[Difference] = Field(default_factory=lambda: [])
    """差异样例：名额按 kind 分配，``bar_mismatch`` 再按字段分配，每个名额最多 ``max_examples`` 条
    （保留最先遇到的）；输出统一按 kind、证券、日期、字段排序。"""
    examples_truncated: bool = False


# ---------------------------------------------------------------- 任务


def data_cross_check(settings: Settings, *, limits: Limits | None = None) -> TaskType:
    def handler(ctx: TaskContext, params: CrossCheckParams) -> CrossCheckResult:
        closed = last_closed_day()
        end = params.end or closed
        if end > closed:
            raise CrossCheckError(
                f"end（{end}）还没有收盘：盘中的日线不是最终值，不能核对。"
                f"最近一个已收盘的日子是 {closed}"
            )
        if end < params.start:
            raise CrossCheckError(f"start（{params.start}）晚于最近一个已收盘的日子（{end}）")
        try:
            found = {p: find_datasource(settings, p) for p in (params.plugin_a, params.plugin_b)}
        except SyncError as exc:
            raise CrossCheckError(str(exc)) from exc
        endpoints = EndpointStore(settings.plugin_config_path)
        run_limits = limits or Limits.from_env()
        checker = _Checker(ctx, params, end)
        with ExitStack() as stack:
            cancel = stack.enter_context(_cancel_watch(ctx))
            for label, plugin_id in (("a", params.plugin_a), ("b", params.plugin_b)):
                entry, deps = found[plugin_id]
                assert entry.manifest is not None
                addresses = endpoints.addresses_for(entry.manifest)
                source = checker.sources[label]
                source.addresses = addresses

                def plugin_log(line: str, pid: str = plugin_id) -> None:
                    ctx.log(f"[{pid}] {line}")

                try:
                    source.session = stack.enter_context(
                        session_for(
                            entry,
                            endpoints,
                            deps=deps,
                            limits=run_limits,
                            cancel=cancel,
                            log=plugin_log,
                        )
                    )
                except PluginRunError as exc:
                    checker.fail(source, exc)
            return checker.run()

    return TaskType(
        name="data.cross_check",
        params_model=CrossCheckParams,
        handler=handler,
        description="只读：比较两个行情源插件的日线 OHLCV 和复权因子，列出差异和只在一边出现的日期",
        max_attempts=1,
    )


@dataclass
class _Source:
    label: str
    plugin_id: str
    session: PluginSession | None = None
    addresses: Mapping[str, str] = field(default_factory=lambda: {})
    caps: Capabilities | None = None
    error: str | None = None
    batch_errors: int = 0
    last_batch_error: str | None = None

    @property
    def usable(self) -> bool:
        return self.session is not None and self.error is None


class _Checker:
    def __init__(self, ctx: TaskContext, params: CrossCheckParams, end: date) -> None:
        self.ctx = ctx
        self.p = params
        self.end = end
        self.sources = {
            "a": _Source("a", params.plugin_a),
            "b": _Source("b", params.plugin_b),
        }
        self.counts: Counter[str] = Counter()
        self.per_symbol: Counter[str] = Counter()
        self.examples: dict[str, list[Difference]] = {}
        self.unknown: dict[str, dict[str, str]] = {}
        self.unknown_fields: Counter[str] = Counter()
        self.bars_compared = 0
        self.bars_consistent = 0
        self.compared = 0

    # ---- 错误

    def fail(self, source: _Source, exc: BaseException, *, batch: bool = False) -> None:
        """记下一个数据源的读取失败（未知）。取消照常抛出，让任务系统处理。"""
        if isinstance(exc, PluginRunError) and exc.reason == "cancelled":
            self.ctx.check_cancelled()
        message = redact(f"{type(exc).__name__}: {exc}", source.addresses)[:_MAX_REASON]
        if isinstance(exc, PluginRunError) and exc.traceback:
            self.ctx.log(exc.traceback, "error")
        if batch:
            source.batch_errors += 1
            source.last_batch_error = message
        else:
            source.error = message
        self.ctx.log(
            f"数据源 {source.label}（{source.plugin_id}）读取失败，结果记为未知：{message}",
            "warning",
        )

    def _mark_unknown(self, symbols: Sequence[str], label: str, reason: str) -> None:
        for s in symbols:
            self.unknown.setdefault(s, {})[label] = reason

    # ---- 主流程

    def run(self) -> CrossCheckResult:
        ctx, p = self.ctx, self.p
        ctx.log(f"交叉核对 {p.plugin_a}（a）和 {p.plugin_b}（b）：{p.start} 至 {self.end}")
        ctx.progress(0.0, "检查数据源")
        for source in self.sources.values():
            self._prepare(source)

        only_a: list[str] = []
        only_b: list[str] = []
        symbols: list[str]
        if p.symbols is not None:
            symbols = list(p.symbols)
        elif all(s.usable for s in self.sources.values()):
            symbols, only_a, only_b = self._common_symbols()
        else:
            symbols = []
            ctx.log("没有传 symbols，而数据源不可用，无法确定要核对的证券", "warning")

        batches = [symbols[i : i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
        for i, batch in enumerate(batches):
            ctx.progress(0.05 + 0.9 * i / len(batches), f"核对 {i * BATCH_SIZE}/{len(symbols)}")
            self._compare_batch(batch)
        ctx.progress(1.0, "完成")
        return self._result(symbols, only_a, only_b)

    def _prepare(self, source: _Source) -> None:
        """连接检查和能力检查。失败记为这个数据源不可用。"""
        if source.session is None:
            return
        try:
            check_connection(source.session, source.addresses)
            source.caps = check_capabilities(source.plugin_id, source.session.call("capabilities"))
        except (PluginRunError, SyncError) as exc:
            self.fail(source, exc)

    def _securities(self, source: _Source) -> pl.DataFrame | None:
        assert source.session is not None and source.caps is not None
        try:
            df = source.session.call("securities", include_delisted=source.caps.lists_delisted)
            if not isinstance(df, pl.DataFrame):
                raise SyncError(f"securities 应返回 DataFrame，实际为 {type(df).__name__}")
            result = check_securities(df)
            if not result.passed:
                raise SyncError(f"证券列表不合格：{'；'.join(result.details)}")
        except (PluginRunError, SyncError) as exc:
            self.fail(source, exc)
            return None
        return known_securities(df)

    def _common_symbols(self) -> tuple[list[str], list[str], list[str]]:
        frames = {label: self._securities(s) for label, s in self.sources.items()}
        fa, fb = frames["a"], frames["b"]
        if fa is None or fb is None:
            return [], [], []
        sa = set(symbols_in_range(fa, self.p.start, self.end))
        sb = set(symbols_in_range(fb, self.p.start, self.end))
        only_a, only_b = sorted(sa - sb), sorted(sb - sa)
        self.ctx.log(f"证券：a {len(sa)} 只，b {len(sb)} 只，交集 {len(sa & sb)} 只")
        return sorted(sa & sb), only_a, only_b

    # ---- 一批证券

    def _fetch(
        self, source: _Source, symbols: Sequence[str]
    ) -> tuple[pl.DataFrame, pl.DataFrame] | None:
        """取这一批的日线和复权因子并校验；失败返回 None（这批在这个数据源上是未知）。"""
        if not source.usable:
            return None
        assert source.session is not None
        try:
            bars = source.session.call("bars", list(symbols), Freq.DAY, self.p.start, self.end)
            _check_bars(bars, symbols, self.p.start, self.end)
            factors = source.session.call("adj_factors", list(symbols), EARLIEST, self.end)
            _check_factors(factors, symbols, self.end)
        except (PluginRunError, SyncError) as exc:
            self.fail(source, exc, batch=True)
            return None
        return bars, factors

    def _compare_batch(self, batch: Sequence[str]) -> None:
        self.ctx.check_cancelled()
        got = {label: self._fetch(source, batch) for label, source in self.sources.items()}
        a, b = got["a"], got["b"]
        for label, value in got.items():
            if value is None:
                source = self.sources[label]
                reason = source.error or source.last_batch_error or "这一批的读取或校验失败"
                self._mark_unknown(batch, label, reason)
        if a is None or b is None:
            return
        self.compared += len(batch)
        self._compare_bars(a[0], b[0])
        self._compare_factors(a[1], b[1])

    def _compare_bars(self, bars_a: pl.DataFrame, bars_b: pl.DataFrame) -> None:
        a = bars_a.filter(pl.col("volume").is_null() | (pl.col("volume") != 0.0))
        b = bars_b.filter(pl.col("volume").is_null() | (pl.col("volume") != 0.0))
        fields = [*PRICE_FIELDS, "volume"]
        # NaN 按空值处理（“不知道”）：NaN 比较永远为假，不转成空会被当成没有差异
        a = a.select(
            "symbol",
            "date",
            pl.lit(True).alias("_a"),
            *[pl.col(f).fill_nan(None).alias(f"{f}_a") for f in fields],
        )
        b = b.select(
            "symbol",
            "date",
            pl.lit(True).alias("_b"),
            *[pl.col(f).fill_nan(None).alias(f"{f}_b") for f in fields],
        )
        joined = a.join(b, on=["symbol", "date"], how="full", coalesce=True).sort("symbol", "date")

        for kind, present, absent in (
            ("only_in_a", "_a", "_b"),
            ("only_in_b", "_b", "_a"),
        ):
            side = joined.filter(pl.col(present).is_not_null() & pl.col(absent).is_null())
            self._add(kind, side.select("symbol", "date"))

        both = joined.filter(pl.col("_a").is_not_null() & pl.col("_b").is_not_null())
        self.bars_compared += both.height
        unit_symbols = self._unit_mismatches(both)
        consistent = pl.lit(True)
        for f in fields:
            va, vb = pl.col(f"{f}_a"), pl.col(f"{f}_b")
            self.unknown_fields[f] += both.filter(va.is_null() | vb.is_null()).height
            tol = self.p.volume_tolerance if f == "volume" else self.p.price_tolerance
            rel = _rel(va, vb)
            bad = both.filter(va.is_not_null() & vb.is_not_null() & (rel > tol))
            ok = va.is_not_null() & vb.is_not_null() & (rel <= tol)
            if f == "volume":
                bad = bad.filter(~pl.col("symbol").is_in(list(unit_symbols)))
                ok = ok & ~pl.col("symbol").is_in(list(unit_symbols))
            consistent = consistent & ok
            self._add(
                "bar_mismatch",
                bad.select(
                    "symbol",
                    "date",
                    pl.lit(f).alias("field"),
                    va.alias("a"),
                    vb.alias("b"),
                    rel.alias("rel_diff"),
                ),
                field=f,
            )
        # 一致的行：五个字段两边都知道、都在阈值内。有空值的行不算一致
        self.bars_consistent += both.filter(consistent).height

    def _unit_mismatches(self, both: pl.DataFrame) -> set[str]:
        """成交量单位疑似不同（几乎恒为 100 倍或 1/100）的证券；每个证券记一条差异。"""
        valid = both.filter(
            pl.col("volume_a").is_not_null()
            & pl.col("volume_b").is_not_null()
            & (pl.col("volume_a") > 0)
            & (pl.col("volume_b") > 0)
        )
        r = pl.col("volume_a") / pl.col("volume_b")
        agg = [
            pl.len().alias("n"),
            r.median().alias("ratio"),
            pl.col("volume_a").median().alias("a"),
            pl.col("volume_b").median().alias("b"),
        ]
        for i, target in enumerate(UNIT_RATIOS):
            agg.append(((r / target - 1).abs() <= UNIT_RATIO_TOLERANCE).sum().alias(f"near{i}"))
        stats = valid.group_by("symbol").agg(agg).filter(pl.col("n") >= UNIT_MIN_ROWS)
        # 先匹配的比值优先（100 在前）
        target = pl.lit(None, dtype=pl.Float64)
        near = pl.lit(0, dtype=pl.UInt32)
        for i, t in reversed(list(enumerate(UNIT_RATIOS))):
            hit = pl.col(f"near{i}") >= UNIT_MIN_SHARE * pl.col("n")
            target = pl.when(hit).then(pl.lit(t)).otherwise(target)
            near = pl.when(hit).then(pl.col(f"near{i}")).otherwise(near)
        flagged = (
            stats.with_columns(target.alias("target"), near.alias("near_rows"))
            .filter(pl.col("target").is_not_null())
            .sort("symbol")
        )
        if flagged.is_empty():
            return set()
        frame = flagged.select(
            "symbol",
            pl.lit(None, dtype=pl.Date).alias("date"),
            pl.lit("volume").alias("field"),
            "a",
            "b",
            pl.lit(None, dtype=pl.Float64).alias("rel_diff"),
            pl.format(
                "成交量单位疑似不同：a/b 的中位数 {}，{}/{} 行接近 {}（契约规定单位是股）",
                pl.col("ratio").round_sig_figs(4),
                pl.col("near_rows"),
                pl.col("n"),
                pl.col("target"),
            ).alias("note"),
        )
        self._add("volume_unit_mismatch", frame)
        return set(frame.get_column("symbol").to_list())

    def _compare_factors(self, fac_a: pl.DataFrame, fac_b: pl.DataFrame) -> None:
        """复权因子是阶梯函数：在两边所有除权日上比较各自生效的因子（第一行之前为 1.0）。"""
        union = (
            pl.concat([fac_a.select("symbol", "date"), fac_b.select("symbol", "date")])
            .unique()
            .sort("symbol", "date")
        )
        if union.is_empty():
            return

        def step(df: pl.DataFrame, name: str) -> pl.DataFrame:
            right = df.select("symbol", "date", pl.col("factor").alias(name)).sort("symbol", "date")
            right = right.with_columns(pl.col("date").alias(f"_{name}_date"))
            return union.join_asof(
                right, on="date", by="symbol", strategy="backward", check_sortedness=False
            )

        sa = step(fac_a, "a")
        sb = step(fac_b, "b")
        joined = sa.join(sb.select("symbol", "date", "b", "_b_date"), on=["symbol", "date"])
        joined = joined.with_columns(
            pl.col("a").fill_null(1.0),
            pl.col("b").fill_null(1.0),
            (pl.col("_a_date") == pl.col("date")).fill_null(False).alias("in_a"),
            (pl.col("_b_date") == pl.col("date")).fill_null(False).alias("in_b"),
        )
        rel = _rel(pl.col("a"), pl.col("b"))
        bad = joined.filter(rel > self.p.factor_tolerance)
        self._add(
            "factor_mismatch",
            bad.select(
                "symbol",
                "date",
                pl.lit("factor").alias("field"),
                "a",
                "b",
                rel.alias("rel_diff"),
                pl.when(pl.col("in_a") & ~pl.col("in_b"))
                .then(pl.lit("这一天只有 a 有除权记录"))
                .when(~pl.col("in_a") & pl.col("in_b"))
                .then(pl.lit("这一天只有 b 有除权记录"))
                .otherwise(pl.lit(None, dtype=pl.String))
                .alias("note"),
            ),
        )

    # ---- 记录

    def _add(self, kind: str, rows: pl.DataFrame, *, field: str | None = None) -> None:
        if rows.is_empty():
            return
        self.counts[kind] += rows.height
        if field is not None:
            self.counts[f"{kind}.{field}"] += rows.height
        for symbol, n in rows.get_column("symbol").value_counts().iter_rows():
            self.per_symbol[symbol] += n
        # 名额按 kind 分配；bar_mismatch 再按字段分配，免得开盘价的差异占满名额
        kept = self.examples.setdefault(f"{kind}.{field}" if field else kind, [])
        room = self.p.max_examples - len(kept)
        if room > 0:
            for row in rows.head(room).iter_rows(named=True):
                kept.append(Difference(kind=kind, **row))  # type: ignore[arg-type]

    def _result(
        self, symbols: Sequence[str], only_a: Sequence[str], only_b: Sequence[str]
    ) -> CrossCheckResult:
        p = self.p
        unknown = sorted(self.unknown.items())
        if not self.compared:
            status = "unknown"
        elif unknown:
            status = "partial"
        else:
            status = "compared"
            if any(self.unknown_fields.values()):
                status = "partial"  # 有字段为空没有比较：不能说两边一致
        if p.symbols is None and not symbols:
            status = "unknown"  # 没有确定要核对哪些证券
        examples = sorted(
            (d for kept in self.examples.values() for d in kept),
            key=lambda d: (d.kind, d.symbol, d.date or date.min, d.field or ""),
        )
        truncated = any(self.counts[k] > len(v) for k, v in self.examples.items())
        top = self.per_symbol.most_common(p.max_examples)
        self.ctx.log(
            f"核对完成：状态 {status}；比较 {self.compared}/{len(symbols)} 只；"
            f"未知 {len(unknown)} 只；"
            f"差异 {dict(self.counts)}"
        )
        return CrossCheckResult(
            plugin_a=p.plugin_a,
            plugin_b=p.plugin_b,
            start=p.start,
            end=self.end,
            status=status,
            complete=status == "compared",
            sources={
                label: SourceStatus(
                    plugin_id=s.plugin_id,
                    ok=s.usable,
                    error=s.error,
                    batch_errors=s.batch_errors,
                )
                for label, s in self.sources.items()
            },
            symbols_requested=len(symbols),
            symbols_compared=self.compared,
            unknown_symbols_total=len(unknown),
            unknown_symbols=[
                UnknownSymbols(
                    symbol=s,
                    unknown_in=sorted(reasons),
                    reason="；".join(f"数据源 {k}：{v}" for k, v in sorted(reasons.items())),
                )
                for s, reasons in unknown[: p.max_examples]
            ],
            only_listed_in_a=list(only_a[: p.max_examples]),
            only_listed_in_b=list(only_b[: p.max_examples]),
            only_listed_in_a_total=len(only_a),
            only_listed_in_b_total=len(only_b),
            bars_compared=self.bars_compared,
            bars_consistent=self.bars_consistent,
            unknown_fields={k: v for k, v in self.unknown_fields.items() if v},
            counts=dict(self.counts),
            symbols_with_differences=len(self.per_symbol),
            top_symbols=dict(top),
            examples=examples,
            examples_truncated=truncated,
        )


# ---------------------------------------------------------------- 辅助


def _rel(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """相对差异；两边都是 0 时为 0。"""
    scale = pl.max_horizontal(a.abs(), b.abs())
    return pl.when(scale == 0).then(0.0).otherwise((a - b).abs() / scale)


def _check_bars(value: object, symbols: Sequence[str], start: date, end: date) -> None:
    if not isinstance(value, pl.DataFrame):
        raise SyncError(f"bars 应返回 DataFrame，实际为 {type(value).__name__}")
    problems = schema_problems(value, BARS_SCHEMA)
    if problems:
        raise SyncError(f"日线不合格：{'；'.join(problems)}")
    _require(value, symbols, start, end, "日线")


def _check_factors(value: object, symbols: Sequence[str], end: date) -> None:
    if not isinstance(value, pl.DataFrame):
        raise SyncError(f"adj_factors 应返回 DataFrame，实际为 {type(value).__name__}")
    result = check_adj_factors(value)
    if not result.passed:
        raise SyncError(f"复权因子不合格：{'；'.join(result.details)}")
    _require(value, symbols, EARLIEST, end, "复权因子")


def _require(df: pl.DataFrame, symbols: Sequence[str], start: date, end: date, what: str) -> None:
    extra = df.filter(~pl.col("symbol").is_in(list(symbols))).get_column("symbol").unique()
    if extra.len():
        raise SyncError(f"{what}里有没有请求的证券：{extra.sort().head(5).to_list()}")
    outside = df.filter(~pl.col("date").is_between(start, end))
    if outside.height:
        raise SyncError(f"{what}有 {outside.height} 行不在 {start} 至 {end} 之间")


__all__ = [
    "BATCH_SIZE",
    "CrossCheckError",
    "CrossCheckParams",
    "CrossCheckResult",
    "Difference",
    "data_cross_check",
]
