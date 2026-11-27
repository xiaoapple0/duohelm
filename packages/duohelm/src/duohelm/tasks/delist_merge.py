"""多个数据源的退市日：精确的优先，近似的不覆盖精确的（xiaoapple0/duohelm-plugins#4）。

同一个快照可以先后被两个数据源写入。数据源声明 ``Capabilities.exact_delist_date`` 为 False
（如 eltdx）时，它的退市日是近似值，不能覆盖别的数据源给出的精确退市日；精确的数据源
（如 xtdata）写入时则以它为准。规则只看这个声明，不认插件名字。完整规则表见
docs/subsystems/data.md 的“多个数据源的退市日”。

精确数据源说在市、已有行是近似数据源（或来源未知）给的退市日时，以精确数据源为准，按在市处理，
并把两边的数据源和日期交给质量报告（``delist_date_conflicts``）。如果精确数据源漏报了退市，
这会带来幸存者偏差，所以必须留下记录，方便核对。

精确数据源写的在市行，``exact`` 记 True（“精确数据源确认在市”），和精确的退市日一样受保护：
之后近似数据源说它已退市，也不覆盖，同样记一条冲突。所以两种同步顺序的结果相同。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

_MAX_EXAMPLES = 5
UNKNOWN_SOURCE = "来源未知"
"""已有行没有记录来源（``delist_date_quality.source`` 为 null）时，质量报告里写的数据源。"""


@dataclass(frozen=True)
class DelistMerge:
    securities: pl.DataFrame
    """这次同步要写的证券（列和输入相同）；保留已有行的证券用已有的整行。"""
    quality: pl.DataFrame
    """symbol、exact、source，和 securities 一一对应。"""
    kept_exact: list[str]
    """近似数据源给出退市日或在市，已有的别人的精确退市日保留。"""
    reports: pl.DataFrame
    """要交给质量报告的冲突：symbol、exact_source、exact_delist_date（在市为 null）、other_source
    （没有记录来源时为“来源未知”）、other_delist_date。两种：精确数据源说在市而覆盖了别人的
    退市日；近似数据源说已退市而没能覆盖精确数据源确认的在市。"""
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
    # 别人的精确行：精确的退市日，或精确数据源确认在市（exact 为 True 而没有退市日）
    foreign_exact = (
        pl.col("_e_exact").fill_null(False)
        & pl.col("_e_source").is_not_null()
        & (pl.col("_e_source") != source)
    )
    exact_wins = pl.lit(exact)
    # 近似的这次一律不覆盖；精确的这次说在市，不覆盖别人的精确退市日
    keep_exact = foreign_exact & (~exact_wins | (~n_delisted & e_delisted))
    # 精确源说在市，已有行是别的数据源（或来源未知）给的、没有被上面保留的退市日：以精确源为准
    in_market = exact_wins & ~n_delisted & e_delisted & foreign & ~keep_exact
    flagged = joined.with_columns(
        keep_exact.fill_null(False).alias("_keep_exact"),
        in_market.fill_null(False).alias("_in_market"),
        (
            exact_wins
            & n_delisted
            & foreign_exact
            & pl.col("delist_date").ne_missing(pl.col("_e_delist_date"))
        )
        .fill_null(False)
        .alias("_conflict"),
    )
    keep = pl.col("_keep_exact")
    merged = flagged.select(
        "symbol",
        *[
            pl.when(keep).then(pl.col(f"_e_{c}")).otherwise(pl.col(c)).alias(c)
            for c in columns
            if c != "symbol"
        ],
        pl.when(keep)
        .then(pl.col("_e_exact"))
        # 精确数据源写的在市行也记 True：精确数据源确认在市；近似数据源写的在市行为 null
        .otherwise(
            pl.when(n_delisted | exact_wins)
            .then(pl.lit(exact))
            .otherwise(pl.lit(None, dtype=pl.Boolean()))
        )
        .alias("exact"),
        pl.when(keep).then(pl.col("_e_source")).otherwise(pl.lit(source)).alias("source"),
        "_keep_exact",
        "_in_market",
        "_conflict",
        pl.col("_e_source").fill_null(UNKNOWN_SOURCE).alias("_e_source_name"),
        pl.col("_e_source").alias("_e_source_raw"),
        pl.col("_e_delist_date"),
        pl.col("delist_date").alias("_n_delist_date"),
        (pl.col("_keep_exact") & n_delisted & ~e_delisted).alias("_approx_over_in_market"),
    )
    reports = pl.concat(
        [
            merged.filter(pl.col("_in_market")).select(
                "symbol",
                pl.lit(source).alias("exact_source"),
                pl.lit(None, dtype=pl.Date()).alias("exact_delist_date"),
                pl.col("_e_source_name").alias("other_source"),
                pl.col("_e_delist_date").alias("other_delist_date"),
            ),
            merged.filter(pl.col("_approx_over_in_market")).select(
                "symbol",
                pl.col("_e_source_raw").alias("exact_source"),
                pl.lit(None, dtype=pl.Date()).alias("exact_delist_date"),
                pl.lit(source).alias("other_source"),
                pl.col("_n_delist_date").alias("other_delist_date"),
            ),
        ]
    ).sort("symbol")

    def symbols(flag: str) -> list[str]:
        return merged.filter(pl.col(flag)).get_column("symbol").sort().to_list()

    return DelistMerge(
        securities=merged.select(columns),
        quality=merged.select("symbol", "exact", "source"),
        kept_exact=symbols("_keep_exact"),
        reports=reports,
        conflicts=symbols("_conflict"),
    )


def examples(symbols: list[str]) -> list[str]:
    return symbols[:_MAX_EXAMPLES]


__all__ = ["UNKNOWN_SOURCE", "DelistMerge", "examples", "merge_delist_dates"]
