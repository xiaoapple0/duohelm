"""契约检查。平台在启用插件前运行；插件作者也可以在自己的 CI 里运行同一套检查。

检查函数接收一个"计算函数" ``compute(data) -> DataFrame``。对因子插件，用
``functools.partial(plugin.compute, factor_id)`` 或 lambda 包一层即可。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from duohelm_sdk.schema import (
    ADJ_FACTORS_SCHEMA,
    FACTOR_OUTPUT_SCHEMA,
    SECURITIES_SCHEMA,
    known_securities,
    schema_problems,
)

ComputeFn = Callable[[pl.DataFrame], pl.DataFrame]

_MAX_EXAMPLES = 5


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: tuple[str, ...] = ()


def check_factor_output(out: pl.DataFrame) -> CheckResult:
    """因子输出格式：列为 date、symbol、value，类型正确，(date, symbol) 非空且不重复。"""
    problems = schema_problems(out, FACTOR_OUTPUT_SCHEMA)
    return CheckResult("factor_output", not problems, tuple(problems))


def check_securities(df: pl.DataFrame) -> CheckResult:
    """证券列表：格式、symbol 不重复、交易所与后缀一致、退市日不早于上市日。

    exchange、kind 是可扩展的枚举：不认识的取值不算错误，这些行由使用方跳过
    （见 :func:`duohelm_sdk.schema.known_securities`），这里只在 details 里说明。
    """
    name = "securities"
    problems = schema_problems(df, SECURITIES_SCHEMA)
    if problems:
        return CheckResult(name, False, tuple(problems))
    known = known_securities(df)
    notes: list[str] = []
    skipped = df.height - known.height
    if skipped:
        examples = df.join(known, on="symbol", how="anti").get_column("symbol").head(_MAX_EXAMPLES)
        notes.append(f"跳过 {skipped} 行不认识的 exchange 或 kind，例如 {examples.to_list()}")
    suffix = pl.col("symbol").str.split(".").list.last()
    rules: list[tuple[str, pl.Expr]] = [
        ("symbol 的后缀必须与 exchange 一致", suffix != pl.col("exchange")),
        ("symbol 必须是 6 位代码加后缀", ~pl.col("symbol").str.contains(r"^\d{6}\.[A-Z]{2}$")),
        ("delist_date 不能早于 list_date", pl.col("delist_date") < pl.col("list_date")),
    ]
    for message, bad in rules:
        rows = known.filter(bad.fill_null(False))
        if rows.height:
            examples = rows.get_column("symbol").head(_MAX_EXAMPLES).to_list()
            problems.append(f"{message}：{rows.height} 行，例如 {examples}")
    return CheckResult(name, not problems, (*problems, *notes))


def check_adj_factors(df: pl.DataFrame) -> CheckResult:
    """复权因子：格式、(date, symbol) 不重复、factor 为有限正数。"""
    name = "adj_factors"
    problems = schema_problems(df, ADJ_FACTORS_SCHEMA)
    if problems:
        return CheckResult(name, False, tuple(problems))
    factor = pl.col("factor")
    bad = df.filter(~(factor.is_finite() & (factor > 0)).fill_null(True))
    if bad.height:
        examples = bad.select("symbol", "date").head(_MAX_EXAMPLES).rows()
        problems.append(f"factor 必须是有限正数：{bad.height} 行，例如 {examples}")
    return CheckResult(name, not problems, tuple(problems))


def check_deterministic(compute: ComputeFn, data: pl.DataFrame) -> CheckResult:
    """同一输入计算两次，结果必须一致。"""
    name = "deterministic"
    try:
        first = compute(data)
        second = compute(data)
    except Exception as exc:
        return CheckResult(name, False, (f"计算出错: {exc!r}",))
    diffs = _value_diffs(first, second, cutoff=None)
    return CheckResult(name, not diffs, tuple(diffs))


def check_no_lookahead(
    compute: ComputeFn,
    data: pl.DataFrame,
    cutoffs: Sequence[date] | None = None,
    *,
    seed: int = 0,
) -> CheckResult:
    """未来函数检查。

    对每个截止日 t，做两种改动，再比较 t 及之前的计算结果是否和原始数据上一致：

    1. 截断：删掉 t 之后的所有行
    2. 扰动：t 之后每一行的数值字段乘以一个随机系数

    任何差异都说明计算用到了 t 之后的数据。

    局限：只能发现"未来数据影响了过去的结果"，抓不到数据本身就带未来信息的情况
    （比如行情源用今天的价格算历史复权因子），那由行情源的契约检查负责。

    data 必须包含 date、symbol 两列。cutoffs 为空时取全部交易日的 25%、50%、75% 分位处。
    """
    name = "no_lookahead"
    if "date" not in data.columns or "symbol" not in data.columns:
        return CheckResult(name, False, ("data 必须包含 date 和 symbol 列",))

    dates: list[date] = data.get_column("date").unique().sort().to_list()
    if len(dates) < 2:
        return CheckResult(name, False, ("至少需要两个交易日的数据",))
    if cutoffs is None:
        picks = {dates[min(len(dates) - 2, int(len(dates) * q))] for q in (0.25, 0.5, 0.75)}
        cutoffs = sorted(picks)

    try:
        baseline = compute(data)
    except Exception as exc:
        return CheckResult(name, False, (f"在原始数据上计算出错: {exc!r}",))

    problems: list[str] = []
    for i, cutoff in enumerate(cutoffs):
        variants = {
            "截断": data.filter(pl.col("date") <= cutoff),
            "扰动": _perturb_after(data, cutoff, seed=seed + i),
        }
        for label, variant in variants.items():
            try:
                result = compute(variant)
            except Exception as exc:
                problems.append(f"截止日 {cutoff}（{label}）计算出错: {exc!r}")
                continue
            problems.extend(
                f"截止日 {cutoff}（{label}）: {d}"
                for d in _value_diffs(baseline, result, cutoff=cutoff)
            )
    return CheckResult(name, not problems, tuple(problems))


def _perturb_after(data: pl.DataFrame, cutoff: date, *, seed: int) -> pl.DataFrame:
    rng = random.Random(seed)
    multipliers = pl.Series(
        "__duohelm_mult", [rng.uniform(0.5, 2.0) for _ in range(data.height)], dtype=pl.Float64
    )
    after = pl.col("date") > cutoff
    exprs = [
        pl.when(after)
        .then((pl.col(col).cast(pl.Float64) * pl.col("__duohelm_mult")).cast(dtype))
        .otherwise(pl.col(col))
        .alias(col)
        for col, dtype in data.schema.items()
        if col not in ("date", "symbol") and dtype.is_numeric()
    ]
    return data.with_columns(multipliers).with_columns(exprs).drop("__duohelm_mult")


def _value_diffs(
    expected: pl.DataFrame,
    actual: pl.DataFrame,
    *,
    cutoff: date | None,
    rtol: float = 1e-9,
    atol: float = 1e-12,
) -> list[str]:
    """比较两份因子输出在 cutoff 及之前的值。NaN 与 null 视为相同（都是缺失）。"""

    def prepare(df: pl.DataFrame, alias: str) -> pl.DataFrame:
        if cutoff is not None:
            df = df.filter(pl.col("date") <= cutoff)
        return df.select(
            "date",
            "symbol",
            pl.col("value").cast(pl.Float64).fill_nan(None).alias(alias),
        )

    joined = prepare(expected, "a").join(
        prepare(actual, "b"), on=["date", "symbol"], how="full", coalesce=True
    )
    differs = (pl.col("a").is_null() != pl.col("b").is_null()) | (
        (pl.col("a") - pl.col("b")).abs() > atol + rtol * pl.col("a").abs()
    )
    bad = joined.filter(differs.fill_null(value=False)).sort("date", "symbol")
    if bad.height == 0:
        return []
    examples = [
        f"{row['date']} {row['symbol']}: {row['a']} → {row['b']}"
        for row in bad.head(_MAX_EXAMPLES).iter_rows(named=True)
    ]
    return [f"{bad.height} 个值不一致，例如 " + "; ".join(examples)]
