from __future__ import annotations

import importlib
import os
import random
from datetime import date, timedelta

import pytest

# 本地缺少 polars 时跳过；在 CI 上必须失败，不能静默跳过。
pl = importlib.import_module("polars") if os.environ.get("CI") else pytest.importorskip("polars")

from duohelm_sdk.checks import (
    check_deterministic,
    check_factor_output,
    check_no_lookahead,
)


def _bars(days: int = 30, symbols: tuple[str, ...] = ("600000.SH", "000001.SZ", "300750.SZ")):
    rng = random.Random(42)
    start = date(2024, 1, 2)
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        price = 10.0
        for i in range(days):
            price *= 1 + rng.uniform(-0.05, 0.05)
            rows.append(
                {
                    "date": start + timedelta(days=i),
                    "symbol": symbol,
                    "close": price,
                    "volume": float(rng.randint(1_000, 100_000)),
                }
            )
    return pl.DataFrame(rows).sort("symbol", "date")


def _out(df, expr):
    return df.select("date", "symbol", expr.cast(pl.Float64).alias("value"))


# ------------------------------------------------------------ 合格的因子


def momentum_5(df):
    """过去 5 根 K 线的收益：只用过去数据。"""
    return _out(df, (pl.col("close") / pl.col("close").shift(5) - 1).over("symbol"))


def cross_section_rank(df):
    """同一天内的横截面排名：只用当天数据。"""
    return _out(df, pl.col("volume").rank().over("date"))


def rolling_mean_10(df):
    return _out(df, pl.col("close").rolling_mean(10).over("symbol"))


# ------------------------------------------------------------ 带未来函数的因子


def next_day_return(df):
    """用到了明天的价格。"""
    return _out(df, (pl.col("close").shift(-1) / pl.col("close") - 1).over("symbol"))


def centered_mean(df):
    """居中滚动窗口：用到了后几天。"""
    return _out(df, pl.col("close").rolling_mean(5, center=True).over("symbol"))


def full_sample_zscore(df):
    """用全样本的均值和标准差做标准化：典型的隐蔽未来函数。"""
    c = pl.col("close")
    return _out(df, ((c - c.mean()) / c.std()).over("symbol"))


def backfilled(df):
    """缺失值用后面的值回填。"""
    c = pl.when(pl.col("volume") > 50_000).then(pl.col("close")).otherwise(None)
    return _out(df, c.fill_null(strategy="backward").over("symbol"))


@pytest.mark.parametrize("compute", [momentum_5, cross_section_rank, rolling_mean_10])
def test_lookahead_check_passes_clean_factors(compute) -> None:
    result = check_no_lookahead(compute, _bars())
    assert result.passed, result.details


@pytest.mark.parametrize(
    "compute", [next_day_return, centered_mean, full_sample_zscore, backfilled]
)
def test_lookahead_check_catches_future_data(compute) -> None:
    result = check_no_lookahead(compute, _bars())
    assert not result.passed
    assert "不一致" in result.details[0]


def test_lookahead_check_with_explicit_cutoff() -> None:
    bars = _bars()
    last = bars.get_column("date").max()
    # 截止日为最后一天时，未来没有数据，任何因子都应通过。
    assert check_no_lookahead(next_day_return, bars, cutoffs=[last]).passed
    assert not check_no_lookahead(next_day_return, bars, cutoffs=[date(2024, 1, 10)]).passed


def test_lookahead_check_reports_compute_errors() -> None:
    def broken(df):
        raise RuntimeError("boom")

    result = check_no_lookahead(broken, _bars())
    assert not result.passed
    assert "boom" in result.details[0]


def test_lookahead_check_needs_date_and_symbol() -> None:
    result = check_no_lookahead(momentum_5, _bars().drop("symbol"))
    assert not result.passed


def test_lookahead_check_needs_two_days() -> None:
    assert not check_no_lookahead(momentum_5, _bars(days=1)).passed


def test_deterministic_check() -> None:
    assert check_deterministic(momentum_5, _bars()).passed

    def noisy(df):
        return _out(df, pl.col("close") * random.random())

    assert not check_deterministic(noisy, _bars()).passed


def test_factor_output_check() -> None:
    bars = _bars()
    assert check_factor_output(momentum_5(bars)).passed

    wrong_columns = bars.select("date", "symbol", pl.col("close").alias("score"))
    assert not check_factor_output(wrong_columns).passed

    wrong_type = bars.select("date", "symbol", pl.col("volume").cast(pl.Int64).alias("value"))
    assert not check_factor_output(wrong_type).passed

    duplicated = pl.concat([momentum_5(bars), momentum_5(bars).head(3)])
    result = check_factor_output(duplicated)
    assert not result.passed
    assert "重复" in result.details[0]
