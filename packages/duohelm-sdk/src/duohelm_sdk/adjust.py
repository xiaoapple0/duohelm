"""复权：由除权除息事件计算复权因子，以及把因子应用到 K 线。

因子的含义见 :data:`duohelm_sdk.schema.ADJ_FACTORS_SCHEMA`：累计后复权比例，自除权除息日
起生效。计算只用除权日**之前**的收盘价和除权日及以前的事件，因此截断某日之后的事件或
行情，该日及以前的因子不变（有测试守护）。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import polars as pl

from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA

EX_EVENTS_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "symbol": pl.String(),
    "cash": pl.Float64(),
    "bonus": pl.Float64(),
    "rights": pl.Float64(),
    "rights_price": pl.Float64(),
}
"""除权除息事件，全部按**每股**计：

- ``cash``：每股派现（元，税前）
- ``bonus``：每股送转股数（送股 + 转增）
- ``rights``：每股配股数
- ``rights_price``：配股价（元）
"""

_PRICE_COLUMNS = ("open", "high", "low", "close")


class InvalidExEventError(ValueError):
    """除权日之前的收盘价或除权参考价不是有限正数，事件或行情数据有误。"""


def factors_from_events(
    closes: pl.DataFrame,
    events: pl.DataFrame,
    *,
    on_invalid: Literal["raise", "skip"] = "raise",
) -> pl.DataFrame:
    """由未复权收盘价和除权除息事件计算复权因子。

    closes：列至少包含 date、symbol、close（未复权）。
    events：格式见 :data:`EX_EVENTS_SCHEMA`。

    对每个事件，取除权日**之前**最近的一个收盘价 P：

        除权参考价 = (P - cash + rights_price × rights) / (1 + bonus + rights)
        比例 = P / 除权参考价

    同一证券同一天的多个事件比例相乘；因子是比例按日期的累乘。除权日之前没有收盘价的
    事件（上市前的事件）不产生因子。返回格式见 ADJ_FACTORS_SCHEMA，按 symbol、date 排序。

    以下事件无效：P 或除权参考价不是有限正数（例如数据源把拿不到的价格填成 0：P 为 0 时比例
    为 0，之后的因子全部变成 0）；除权日之前最近的一行收盘价为 null（有行但价格未知，不是
    上市前）；事件的 cash、bonus、rights、rights_price 有 null（未知，不是 0）。
    ``on_invalid="raise"`` 时抛 :class:`InvalidExEventError`；``"skip"`` 时丢弃这些事件，
    但之后的因子少乘了这次的比例，调用方应另行核对或告警。
    """
    prices = closes.select(
        pl.col("date").cast(pl.Date),
        pl.col("symbol").cast(pl.String),
        pl.col("close").cast(pl.Float64).alias("prev_close"),
        pl.lit(True).alias("_has_close_row"),
    ).sort("date")
    ev = events.select(pl.col(name).cast(dtype) for name, dtype in EX_EVENTS_SCHEMA.items()).sort(
        "date"
    )

    joined = ev.join_asof(
        prices,
        on="date",
        by="symbol",
        strategy="backward",
        allow_exact_matches=False,  # 除权日当天的收盘价已经是除权后的价格
        check_sortedness=False,  # 已按 date 排序；按 by 分组时 polars 无法自行检查
    ).filter(pl.col("_has_close_row").is_not_null())  # 之前有行：不是上市前的事件

    joined = joined.with_columns(
        (
            (pl.col("prev_close") - pl.col("cash") + pl.col("rights_price") * pl.col("rights"))
            / (1 + pl.col("bonus") + pl.col("rights"))
        ).alias("ref")
    )
    valid = (
        pl.col("prev_close").is_finite()
        & (pl.col("prev_close") > 0)
        & pl.col("ref").is_finite()
        & (pl.col("ref") > 0)
    ).fill_null(False)
    invalid = joined.filter(~valid)
    if invalid.height:
        if on_invalid == "raise":
            sample = invalid.select("symbol", "date", "prev_close", "ref").head(5).to_dicts()
            raise InvalidExEventError(
                f"{invalid.height} 个事件的除权前收盘价或除权参考价不是有限正数：{sample}"
            )
        joined = joined.filter(valid)

    return (
        joined.group_by("symbol", "date")
        .agg((pl.col("prev_close") / pl.col("ref")).product().alias("ratio"))
        .sort("symbol", "date")
        .select(
            "date",
            "symbol",
            pl.col("ratio").cum_prod().over("symbol").alias("factor"),
        )
        .select(pl.col(name).cast(dtype) for name, dtype in ADJ_FACTORS_SCHEMA.items())
    )


def factor_as_of(factors: pl.DataFrame, frame: pl.DataFrame) -> pl.Series:
    """frame（含 date、symbol）每一行当天生效的因子；没有因子时为 1.0。按 frame 的行序返回。"""
    keyed = frame.select("date", "symbol").with_row_index("_row").sort("date")
    joined = keyed.join_asof(
        factors.select("date", "symbol", "factor").sort("date"),
        on="date",
        by="symbol",
        strategy="backward",  # 除权日当天的 K 线用新的因子
        check_sortedness=False,
    )
    return joined.sort("_row").get_column("factor").fill_null(1.0)


def adjust_bars(
    bars: pl.DataFrame,
    factors: pl.DataFrame,
    *,
    mode: Literal["hfq", "qfq"],
    base_date: date | None = None,
) -> pl.DataFrame:
    """把复权因子应用到未复权 K 线（BARS_SCHEMA）。成交量和成交额不调整。

    - ``hfq``：后复权，价格 × factor(t)
    - ``qfq``：以 ``base_date`` 为基准的前复权，价格 × factor(t) / factor(base_date)。
      ``base_date`` 为空时用 bars 里的最后一天。研究和回测必须传入当时的日期，
      否则之后发生的除权会改变过去的价格。
    """
    factor = factor_as_of(factors, bars)
    if mode == "hfq":
        scale = factor
    else:
        base = base_date or bars.get_column("date").max()
        symbols = bars.select("symbol").unique()
        base_frame = symbols.with_columns(pl.lit(base).cast(pl.Date).alias("date"))
        base_factor = base_frame.with_columns(
            factor_as_of(factors, base_frame).alias("_base")
        ).select("symbol", "_base")
        scale = (
            bars.select("symbol")
            .join(base_factor, on="symbol", how="left", maintain_order="left")
            .with_columns((factor / pl.col("_base")).alias("_scale"))
            .get_column("_scale")
        )
    return bars.with_columns((pl.col(c) * scale).alias(c) for c in _PRICE_COLUMNS).select(
        list(BARS_SCHEMA)
    )
