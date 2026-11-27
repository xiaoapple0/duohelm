"""多个数据源的退市日：精确的优先，近似的不覆盖精确的（xiaoapple0/duohelm-plugins#4）。

同一个快照可以先后被两个数据源写入。数据源声明 ``Capabilities.exact_delist_date`` 为 False
（如 eltdx）时，它的退市日是近似值，不能覆盖别的数据源给出的精确退市日；精确的数据源
（如 xtdata）写入时则以它为准。规则只看这个声明，不认插件名字。完整规则表见
docs/subsystems/data.md 的“多个数据源的退市日”。

“不知道”不当成“在市”：精确数据源说在市、近似数据源说退市时，按已退市处理。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

_MAX_EXAMPLES = 5


@dataclass(frozen=True)
class DelistMerge:
    securities: pl.DataFrame
    """这次同步要写的证券（列和输入相同）；保留已有行的证券用已有的整行。"""
    quality: pl.DataFrame
    """symbol、exact、source，和 securities 一一对应。"""
    kept_exact: list[str]
    """近似数据源给出退市日或在市，已有的别人的精确退市日保留。"""
    kept_delisted: list[str]
    """精确数据源说在市，已有的近似退市日保留（按已退市处理）。"""
    conflicts: list[str]
    """两个精确数据源的退市日不同：以这次同步的为准。"""


def merge_delist_dates(
    new: pl.DataFrame,
    existing: pl.DataFrame,
    existing_quality: pl.DataFrame,
    *,
    exact: bool,
    source: str,
) -> DelistMerge:
    """合并这次同步的证券和当前快照里已有的。existing、existing_quality 可以为空表。"""
    columns = new.columns
    old = existing.join(
        existing_quality.select(
            "symbol",
            pl.col("exact").alias("_e_exact"),
            pl.col("source").alias("_e_source"),
        ),
        on="symbol",
        how="left",
    ).select(
        "symbol",
        *[pl.col(c).alias(f"_e_{c}") for c in columns if c != "symbol"],
        "_e_exact",
        "_e_source",
    )
    joined = new.join(old, on="symbol", how="left")
    e_delisted = pl.col("_e_delist_date").is_not_null()
    n_delisted = pl.col("delist_date").is_not_null()
    foreign = pl.col("_e_source").is_null() | (pl.col("_e_source") != source)
    # 已有行里 source 为空的精确行不受保护：SDK 0.4.1 之前的近似数据源写出的也是 exact=True
    foreign_exact = (
        e_delisted
        & pl.col("_e_exact").fill_null(False)
        & pl.col("_e_source").is_not_null()
        & (pl.col("_e_source") != source)
    )
    foreign_approx = e_delisted & ~pl.col("_e_exact").fill_null(False) & foreign
    exact_wins = pl.lit(exact)
    keep_exact = foreign_exact & ((~n_delisted) | ~exact_wins)
    # 来源未知的已退市行（不管 exact 是什么）也保留：它可能是 SDK 0.4.1 之前的近似数据源写出的
    # “假精确”。不知道它是不是精确，不能因为精确源说在市就改成在市
    unknown_source = e_delisted & pl.col("_e_source").is_null()
    keep_delisted = exact_wins & ~n_delisted & (foreign_approx | unknown_source)
    flagged = joined.with_columns(
        keep_exact.fill_null(False).alias("_keep_exact"),
        keep_delisted.fill_null(False).alias("_keep_delisted"),
        (
            exact_wins
            & n_delisted
            & foreign_exact
            & (pl.col("delist_date") != pl.col("_e_delist_date"))
        )
        .fill_null(False)
        .alias("_conflict"),
    )
    keep = pl.col("_keep_exact") | pl.col("_keep_delisted")
    merged = flagged.select(
        "symbol",
        *[
            pl.when(keep).then(pl.col(f"_e_{c}")).otherwise(pl.col(c)).alias(c)
            for c in columns
            if c != "symbol"
        ],
        pl.when(keep)
        .then(
            # 来源未知的“精确”行不能当精确：保留时记为近似
            pl.when(pl.col("_e_exact").fill_null(False) & pl.col("_e_source").is_null())
            .then(pl.lit(False))
            .otherwise(pl.col("_e_exact"))
        )
        .otherwise(
            pl.when(n_delisted).then(pl.lit(exact)).otherwise(pl.lit(None, dtype=pl.Boolean()))
        )
        .alias("exact"),
        pl.when(keep).then(pl.col("_e_source")).otherwise(pl.lit(source)).alias("source"),
        "_keep_exact",
        "_keep_delisted",
        "_conflict",
    )

    def symbols(flag: str) -> list[str]:
        return merged.filter(pl.col(flag)).get_column("symbol").sort().to_list()

    return DelistMerge(
        securities=merged.select(columns),
        quality=merged.select("symbol", "exact", "source"),
        kept_exact=symbols("_keep_exact"),
        kept_delisted=symbols("_keep_delisted"),
        conflicts=symbols("_conflict"),
    )


def examples(symbols: list[str]) -> list[str]:
    return symbols[:_MAX_EXAMPLES]


__all__ = ["DelistMerge", "examples", "merge_delist_dates"]
