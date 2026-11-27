"""入库质量报告（#31）：同步时对这次取到的日线和复权因子做数据质量检查，随快照保存。

设计见 docs/subsystems/data.md 的“入库质量报告”一节。要点：

- 只报告，不阻止提交。哪些问题要阻止由维护者决定（#67）
- 检查的范围是这次同步取到的数据（[start, end] 内的日线，从 1990-12-19 起的复权因子），
  不是整个快照；增量同步只检查新取的那一段
- 拿不到检查所需的数据时，那一项的状态是 ``unavailable``，写明原因，**不是** ``ok``：
  没有除权除息记录就无法核对复权因子的跳变，停牌日列表拿不到就分不清缺的日子是停牌还是缺数据
- 报告是确定的：同样的数据得到同样的报告（不含时间戳），所以重复同步不会因为报告产生新快照
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

import polars as pl

REPORT_VERSION = 1
MAX_EXAMPLES = 20
"""每项检查最多保存的问题样例。"""
REPORT_KEY = "sync"
"""``quality_report`` 数据集里报告所在的行：每次同步覆盖上一次的报告。"""
_FACTOR_EPS = 1e-9

Status = Literal["ok", "issues", "unavailable"]

_PRICES = ("open", "high", "low", "close")


class QualityCollector:
    """逐批累积检查结果；全部批次处理完后用 :meth:`finish` 得到报告。

    calendar：交易日历（有 date 列），范围 [start, end]。securities：证券列表（symbol、
    list_date、delist_date），用来确定每只证券应有的交易日。ex_events：除权除息事件
    （``EX_EVENTS_SCHEMA`` 格式）；None 表示数据里没有，复权跳变一项报告“无法检查”。
    """

    def __init__(
        self,
        calendar: pl.DataFrame,
        securities: pl.DataFrame,
        start: date,
        end: date,
        *,
        ex_events: pl.DataFrame | None = None,
        ex_events_missing_reason: str = "数据源没有提供除权除息记录",
    ) -> None:
        self._calendar = calendar.select("date").unique()
        self._securities = securities.select("symbol", "list_date", "delist_date")
        self._start = start
        self._end = end
        self._events = ex_events
        self._events_reason = ex_events_missing_reason
        self._symbols = 0
        self._bars_rows = 0
        # OHLC
        self._ohlc_counts = {"missing_price": 0, "non_positive_price": 0}
        self._ohlc_counts |= {"high_below_open_close": 0, "low_above_open_close": 0}
        self._ohlc_rows = 0
        self._ohlc_symbols: set[str] = set()
        self._ohlc_examples: list[dict[str, Any]] = []
        # 日期不在日历里
        self._cal_rows = 0
        self._cal_symbols: set[str] = set()
        self._cal_examples: list[dict[str, Any]] = []
        # 缺失的交易日
        self._missing: list[dict[str, Any]] = []
        self._expected_days = 0
        # 复权跳变
        self._factor_rows = 0
        self._no_event: list[dict[str, Any]] = []
        self._no_factor: list[dict[str, Any]] = []
        self._no_event_n = 0
        self._no_factor_n = 0

    # ---- 逐批累积 ----

    def add_batch(
        self,
        symbols: Sequence[str],
        bars: pl.DataFrame,
        placeholders: pl.DataFrame,
        factors: pl.DataFrame,
    ) -> None:
        """一批证券的结果。bars 是入库的日线（成交量不为 0），placeholders 是被丢弃的
        成交量为 0 的占位行（已知的停牌日），factors 是这批证券的复权因子。"""
        self._symbols += len(symbols)
        self._bars_rows += bars.height
        self._check_ohlc(bars)
        self._check_calendar(bars)
        self._check_missing(symbols, bars, placeholders)
        self._check_factors(symbols, factors)

    def _check_ohlc(self, bars: pl.DataFrame) -> None:
        o, h, low, c = (pl.col(p) for p in _PRICES)
        any_price = pl.any_horizontal(*(pl.col(p).is_null() | pl.col(p).is_nan() for p in _PRICES))
        flags = {
            "missing_price": any_price,
            "non_positive_price": pl.any_horizontal(*(pl.col(p) <= 0 for p in _PRICES)).fill_null(
                False
            ),
            # 价格缺失或非正的行，高低价的比较没有意义，只记前两类
            "high_below_open_close": (~any_price) & (h < pl.max_horizontal(o, c)),
            "low_above_open_close": (~any_price) & (low > pl.min_horizontal(o, c)),
        }
        marked = bars.select("symbol", "date", **flags)
        bad = marked.filter(pl.any_horizontal(*flags.keys()))
        if not bad.height:
            return
        for name in flags:
            self._ohlc_counts[name] += int(bad.get_column(name).sum())
        self._ohlc_rows += bad.height
        self._ohlc_symbols.update(bad.get_column("symbol").unique().to_list())
        for row in bad.sort("symbol", "date").head(MAX_EXAMPLES).iter_rows(named=True):
            if len(self._ohlc_examples) >= MAX_EXAMPLES:
                break
            problems = [name for name in flags if row[name]]
            self._ohlc_examples.append(_example(row, problems))

    def _check_calendar(self, bars: pl.DataFrame) -> None:
        off = bars.join(self._calendar, on="date", how="anti")
        if not off.height:
            return
        self._cal_rows += off.height
        self._cal_symbols.update(off.get_column("symbol").unique().to_list())
        for row in off.sort("symbol", "date").head(MAX_EXAMPLES).iter_rows(named=True):
            if len(self._cal_examples) >= MAX_EXAMPLES:
                break
            self._cal_examples.append(_example(row, ["date_not_in_calendar"]))

    def _check_missing(
        self, symbols: Sequence[str], bars: pl.DataFrame, placeholders: pl.DataFrame
    ) -> None:
        """每只证券应有的交易日：[max(start, 上市日), min(end, 退市日)] 内的交易日。
        没有 K 线、也没有成交量为 0 的占位行的日子算缺失；占位行是已知的停牌，单独计数。"""
        bounds = pl.DataFrame({"symbol": list(symbols)}, schema={"symbol": pl.String()}).join(
            self._securities, on="symbol", how="left"
        )
        lo = pl.max_horizontal(pl.lit(self._start), pl.col("list_date"))  # 忽略 null
        hi = pl.min_horizontal(pl.lit(self._end), pl.col("delist_date"))
        expected = (
            bounds.select("symbol", lo.alias("lo"), hi.alias("hi"))
            .join(self._calendar, how="cross")
            .filter(pl.col("date").is_between(pl.col("lo"), pl.col("hi")))
            .select("symbol", "date")
        )
        self._expected_days += expected.height
        have = bars.select("symbol", "date")
        stub = placeholders.select("symbol", "date")
        missing = expected.join(have, on=["symbol", "date"], how="anti").join(
            stub, on=["symbol", "date"], how="anti"
        )
        known = expected.join(stub, on=["symbol", "date"], how="semi")
        per_missing = missing.group_by("symbol").len(name="missing")
        per_known = known.group_by("symbol").len(name="placeholder")
        merged = per_missing.join(per_known, on="symbol", how="full", coalesce=True).select(
            "symbol",
            pl.col("missing").fill_null(0),
            pl.col("placeholder").fill_null(0),
        )
        merged = merged.filter((pl.col("missing") > 0) | (pl.col("placeholder") > 0))
        self._missing += merged.sort("symbol").to_dicts()

    def _check_factors(self, symbols: Sequence[str], factors: pl.DataFrame) -> None:
        self._factor_rows += factors.height
        if self._events is None:
            return
        batch = list(symbols)
        events = self._events.filter(pl.col("symbol").is_in(batch)).select("symbol", "date")
        ordered = factors.sort("symbol", "date").with_columns(
            (pl.col("factor") / pl.col("factor").shift(1).over("symbol").fill_null(1.0)).alias(
                "ratio"
            )
        )
        jumps = ordered.filter((pl.col("ratio") - 1.0).abs() > _FACTOR_EPS).select("symbol", "date")
        no_event = jumps.join(events, on=["symbol", "date"], how="anti")
        no_factor = events.unique().join(
            ordered.select("symbol", "date"), on=["symbol", "date"], how="anti"
        )
        self._no_event_n += no_event.height
        self._no_factor_n += no_factor.height
        for frame, bucket in ((no_event, self._no_event), (no_factor, self._no_factor)):
            for row in frame.sort("symbol", "date").head(MAX_EXAMPLES).iter_rows(named=True):
                if len(bucket) < MAX_EXAMPLES:
                    bucket.append({"symbol": row["symbol"], "date": row["date"].isoformat()})

    # ---- 报告 ----

    def finish(self, plugin_id: str) -> dict[str, Any]:
        """生成报告（可以直接 json 序列化，不含时间戳）。"""
        ohlc_status: Status = "issues" if self._ohlc_rows else "ok"
        cal_status: Status = "issues" if self._cal_rows else "ok"
        missing_total = sum(m["missing"] for m in self._missing)
        placeholder_total = sum(m["placeholder"] for m in self._missing)
        if self._events is None:
            adj: dict[str, Any] = {
                "status": "unavailable",
                "reason": self._events_reason,
                "factor_rows": self._factor_rows,
            }
        else:
            adj = {
                "status": "issues" if self._no_event_n or self._no_factor_n else "ok",
                "factor_rows": self._factor_rows,
                "factor_jumps_without_event": self._no_event_n,
                "events_without_factor": self._no_factor_n,
                "examples_factor_jumps_without_event": self._no_event,
                "examples_events_without_factor": self._no_factor,
            }
        return {
            "version": REPORT_VERSION,
            "plugin_id": plugin_id,
            "start": self._start.isoformat(),
            "end": self._end.isoformat(),
            "symbols": self._symbols,
            "bars_rows": self._bars_rows,
            "trading_days": self._calendar.height,
            "checks": {
                "ohlc": {
                    "status": ohlc_status,
                    "rows_checked": self._bars_rows,
                    "rows_with_issues": self._ohlc_rows,
                    "issues": dict(self._ohlc_counts),
                    "symbols_affected": len(self._ohlc_symbols),
                    "examples": self._ohlc_examples,
                },
                "calendar_dates": {
                    "status": cal_status,
                    "rows_not_in_calendar": self._cal_rows,
                    "symbols_affected": len(self._cal_symbols),
                    "examples": self._cal_examples,
                },
                "adj_factor_jumps": adj,
                "missing_days": {
                    "status": "issues" if missing_total else "ok",
                    # 数据源没有提供停牌日列表：没有 K 线又没有占位行的日子，可能是停牌，
                    # 也可能是缺数据，无法区分，都计入 missing
                    "suspension": "unknown",
                    "suspension_note": (
                        "数据源没有提供停牌日列表。只能识别成交量为 0 的占位行（已扣除，"
                        "计入 placeholder）；其余没有 K 线的日子可能是停牌，也可能是缺数据，"
                        "无法区分，计入 missing"
                    ),
                    "expected_days": self._expected_days,
                    "missing_total": missing_total,
                    "placeholder_total": placeholder_total,
                    "symbols_with_missing": sum(1 for m in self._missing if m["missing"]),
                    "per_symbol": self._missing,
                },
            },
        }


def report_statuses(report: dict[str, Any]) -> dict[str, str]:
    """每项检查的状态：检查名 -> ok / issues / unavailable。"""
    return {name: str(check["status"]) for name, check in report["checks"].items()}


def encode_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def report_frame(report: dict[str, Any]) -> pl.DataFrame:
    """``quality_report`` 数据集的行。"""
    return pl.DataFrame({"key": [REPORT_KEY], "report": [encode_report(report)]})


def _example(row: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    return {"symbol": row["symbol"], "date": row["date"].isoformat(), "problems": problems}


__all__ = [
    "MAX_EXAMPLES",
    "REPORT_KEY",
    "REPORT_VERSION",
    "QualityCollector",
    "Status",
    "encode_report",
    "report_frame",
    "report_statuses",
]
