from __future__ import annotations

import importlib
import os
import random
from datetime import date, timedelta

import pytest

# 本地缺少 polars 时跳过；在 CI 上必须失败，不能静默跳过。
pl = importlib.import_module("polars") if os.environ.get("CI") else pytest.importorskip("polars")

from duohelm_sdk.adjust import (
    InvalidExEventError,
    adjust_bars,
    factors_from_events,
)
from duohelm_sdk.checks import check_adj_factors
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA

D = [date(2024, 1, 2) + timedelta(days=i) for i in range(10)]


def closes(rows):
    return pl.DataFrame(
        rows, schema={"date": pl.Date, "symbol": pl.String, "close": pl.Float64}, orient="row"
    )


def events(rows):
    return pl.DataFrame(
        rows,
        schema={
            "date": pl.Date,
            "symbol": pl.String,
            "cash": pl.Float64,
            "bonus": pl.Float64,
            "rights": pl.Float64,
            "rights_price": pl.Float64,
        },
        orient="row",
    )


def bars_from(c):
    return c.select(
        "date",
        "symbol",
        *(pl.col("close").alias(x) for x in ("open", "high", "low", "close")),
        pl.lit(100.0).alias("volume"),
        pl.lit(1000.0).alias("amount"),
    ).select(list(BARS_SCHEMA))


# ---- 公式 ----


def test_cash_dividend() -> None:
    c = closes([(D[0], "A", 10.0), (D[1], "A", 10.0), (D[2], "A", 9.0)])
    f = factors_from_events(c, events([(D[2], "A", 1.0, 0.0, 0.0, 0.0)]))
    assert f.schema == ADJ_FACTORS_SCHEMA
    [(day, sym, factor)] = f.rows()
    assert (day, sym) == (D[2], "A")
    assert factor == pytest.approx(10 / 9)  # 参考价 (10 - 1) / 1 = 9


def test_bonus_shares() -> None:
    c = closes([(D[0], "A", 20.0), (D[1], "A", 10.0)])
    f = factors_from_events(c, events([(D[1], "A", 0.0, 1.0, 0.0, 0.0)]))  # 10 送 10
    assert f["factor"].to_list() == [pytest.approx(2.0)]


def test_rights_issue() -> None:
    c = closes([(D[0], "A", 10.0), (D[1], "A", 9.0)])
    f = factors_from_events(c, events([(D[1], "A", 0.0, 0.0, 0.3, 5.0)]))  # 10 配 3，配股价 5
    ref = (10 + 5 * 0.3) / 1.3
    assert f["factor"].to_list() == [pytest.approx(10 / ref)]


def test_uses_close_before_ex_date_not_on_it() -> None:
    c = closes([(D[0], "A", 10.0), (D[1], "A", 1.0)])  # 除权日当天的收盘价不能参与计算
    f = factors_from_events(c, events([(D[1], "A", 1.0, 0.0, 0.0, 0.0)]))
    assert f["factor"].to_list() == [pytest.approx(10 / 9)]


def test_cumulative_and_per_symbol() -> None:
    c = closes(
        [
            (D[0], "A", 10.0),
            (D[1], "A", 10.0),
            (D[2], "A", 10.0),
            (D[0], "B", 20.0),
            (D[1], "B", 20.0),
        ]
    )
    ev = events(
        [
            (D[1], "A", 1.0, 0.0, 0.0, 0.0),
            (D[2], "A", 0.0, 1.0, 0.0, 0.0),
            (D[1], "B", 2.0, 0.0, 0.0, 0.0),
        ]
    )
    f = factors_from_events(c, ev)
    got = {(r[1], r[0]): r[2] for r in f.rows()}
    assert got[("A", D[1])] == pytest.approx(10 / 9)
    assert got[("A", D[2])] == pytest.approx(10 / 9 * 2)
    assert got[("B", D[1])] == pytest.approx(20 / 18)


def test_same_day_events_multiply() -> None:
    c = closes([(D[0], "A", 10.0), (D[1], "A", 4.5)])
    ev = events([(D[1], "A", 1.0, 0.0, 0.0, 0.0), (D[1], "A", 0.0, 1.0, 0.0, 0.0)])
    f = factors_from_events(c, ev)
    assert f.height == 1
    assert f["factor"][0] == pytest.approx(10 / 9 * 2)


def test_event_before_listing_ignored() -> None:
    c = closes([(D[5], "A", 10.0)])
    f = factors_from_events(c, events([(D[1], "A", 1.0, 0.0, 0.0, 0.0)]))
    assert f.height == 0


def test_invalid_event() -> None:
    c = closes([(D[0], "A", 1.0), (D[1], "A", 1.0)])
    bad = events([(D[1], "A", 5.0, 0.0, 0.0, 0.0)])  # 派现超过股价：参考价为负
    with pytest.raises(InvalidExEventError):
        factors_from_events(c, bad)
    assert factors_from_events(c, bad, on_invalid="skip").height == 0


@pytest.mark.parametrize("prev_close", [0.0, -0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_prev_close_is_invalid(prev_close: float) -> None:
    """除权前收盘价不是有限正数（xtdata 对退市股返回的价格全为 0）：配股的参考价仍为正，
    比例 P / ref 会是 0，之后的因子全部变成 0。必须当作无效事件。"""
    c = closes([(D[0], "A", 10.0), (D[1], "A", prev_close), (D[3], "A", 10.0)])
    ev = events(
        [
            (D[2], "A", 0.08, 0.3, 0.4, 10.0),  # 配股：P 为 0 时参考价 (0 - 0.08 + 4) / 1.7 > 0
            (D[4], "A", 1.0, 0.0, 0.0, 0.0),  # 之后一个正常的派现
        ]
    )
    with pytest.raises(InvalidExEventError):
        factors_from_events(c, ev)
    f = factors_from_events(c, ev, on_invalid="skip")
    assert f["date"].to_list() == [D[4]]
    assert f["factor"].to_list() == pytest.approx([10.0 / 9.0])
    assert check_adj_factors(f).passed


def test_null_prev_close_is_invalid_not_before_listing() -> None:
    """除权日前一行的收盘价是 null：价格未知，不能当成“上市前的事件”悄悄丢掉。"""
    c = closes([(D[0], "A", 10.0), (D[1], "A", None)])
    ev = events([(D[2], "A", 1.0, 0.0, 0.0, 0.0)])
    with pytest.raises(InvalidExEventError):
        factors_from_events(c, ev)
    assert factors_from_events(c, ev, on_invalid="skip").height == 0


@pytest.mark.parametrize("field", [2, 3, 4, 5])  # cash、bonus、rights、rights_price
def test_null_event_field_is_invalid(field: int) -> None:
    """事件字段为 null 是“未知”，不是 0。之前比例为 null，product 跳过它，事件等于不存在。"""
    c = closes([(D[0], "A", 10.0)])
    row = [D[1], "A", 1.0, 0.0, 0.0, 0.0]
    row[field] = None
    ev = events([tuple(row)])
    with pytest.raises(InvalidExEventError):
        factors_from_events(c, ev)
    assert factors_from_events(c, ev, on_invalid="skip").height == 0


def test_check_adj_factors() -> None:
    good = pl.DataFrame({"date": [D[0]], "symbol": ["A"], "factor": [1.2]})
    assert check_adj_factors(good).passed
    for value in (0.0, -1.0, float("inf"), float("nan")):
        assert not check_adj_factors(good.with_columns(pl.lit(value).alias("factor"))).passed


# ---- 应用因子 ----


def test_hfq_is_continuous_across_ex_date() -> None:
    c = closes([(D[0], "A", 10.0), (D[1], "A", 10.0), (D[2], "A", 9.0), (D[3], "A", 9.0)])
    f = factors_from_events(c, events([(D[2], "A", 1.0, 0.0, 0.0, 0.0)]))
    out = adjust_bars(bars_from(c), f, mode="hfq")
    assert out.schema == BARS_SCHEMA
    assert out["close"].to_list() == pytest.approx([10.0, 10.0, 10.0, 10.0])
    assert out["volume"].to_list() == [100.0] * 4  # 成交量不调整


def test_qfq_uses_base_date() -> None:
    c = closes([(D[0], "A", 10.0), (D[1], "A", 9.0), (D[2], "A", 9.0)])
    f = factors_from_events(c, events([(D[1], "A", 1.0, 0.0, 0.0, 0.0)]))
    latest = adjust_bars(bars_from(c), f, mode="qfq")
    assert latest["close"].to_list() == pytest.approx([9.0, 9.0, 9.0])
    before = adjust_bars(bars_from(c), f, mode="qfq", base_date=D[0])
    assert before["close"].to_list() == pytest.approx([10.0, 10.0, 10.0])


def test_symbols_without_factors_unchanged() -> None:
    c = closes([(D[0], "A", 10.0), (D[0], "B", 5.0)])
    out = adjust_bars(bars_from(c), factors_from_events(c, events([])), mode="qfq")
    assert out["close"].to_list() == [10.0, 5.0]


# ---- 不引入未来数据 ----


def _random_history(seed: int):
    rng = random.Random(seed)
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(400)]
    rows = []
    ev = []
    for sym in ("600000.SH", "000001.SZ", "920000.BJ"):
        price = rng.uniform(5, 50)
        start = rng.randint(0, 50)
        for i, day in enumerate(days[start:]):
            if rng.random() < 0.03:  # 停牌：这一天没有行
                continue
            ex = rng.random() < 0.02
            if ex:
                cash = round(price * rng.uniform(0, 0.05), 3)
                bonus = rng.choice([0.0, 0.0, 0.3, 1.0])
                rights = rng.choice([0.0, 0.0, 0.0, 0.2])
                rights_price = round(price * 0.6, 2) if rights else 0.0
                ev.append((day, sym, cash, bonus, rights, rights_price))
                price = (price - cash + rights_price * rights) / (1 + bonus + rights)
            price *= 1 + rng.uniform(-0.05, 0.05)
            rows.append((day, sym, round(price, 2)))
            del i
    return closes(rows), events(ev), days


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_factors_unchanged_when_future_events_truncated(seed: int) -> None:
    """截断某日之后的除权记录（以及行情），该日及以前的因子值必须不变。"""
    c, ev, days = _random_history(seed)
    assert ev.height > 5
    full = factors_from_events(c, ev)
    for cutoff in days[50::50]:
        truncated = factors_from_events(
            c.filter(pl.col("date") <= cutoff), ev.filter(pl.col("date") <= cutoff)
        )
        expected = full.filter(pl.col("date") <= cutoff)
        assert truncated.equals(expected), f"{cutoff} 及以前的因子因后续数据而改变"


@pytest.mark.parametrize("seed", [1, 2])
def test_qfq_unchanged_when_future_data_truncated(seed: int) -> None:
    """以 T 日为基准的前复权价，不受 T 日之后的除权和行情影响。"""
    c, ev, days = _random_history(seed)
    bars = bars_from(c)
    full_factors = factors_from_events(c, ev)
    for cutoff in days[100::100]:
        past = pl.col("date") <= cutoff
        a = adjust_bars(bars, full_factors, mode="qfq", base_date=cutoff).filter(past)
        b = adjust_bars(
            bars.filter(past),
            factors_from_events(c.filter(past), ev.filter(past)),
            mode="qfq",
            base_date=cutoff,
        )
        assert a.sort("symbol", "date").equals(b.sort("symbol", "date"))
