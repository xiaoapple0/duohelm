"""每日快照：ST 名单和指数成分（#43）。

xtdata 普通版和 eltdx 都拿不到历史 ST 状态和历史指数成分，只能从现在起每天保存快照。
设计见 docs/subsystems/data.md 的“ST 名单和指数成分快照”一节。要点：

- 快照日期由调用方（:mod:`duohelm.tasks.sync`）决定：最近一个已收盘的交易日。同步区间早于
  它时不写：今天的名单不能当成过去的名单
- 读取失败是“未知”，不是“空”：指数成分取不到、格式不对、为空，都不写，不能写成空名单
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

import polars as pl

from duohelm.isolation.runner import PluginRunError, PluginSession

DEFAULT_INDEXES: tuple[str, ...] = (
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "000016.SH",  # 上证50
    "399006.SZ",  # 创业板指
    "000688.SH",  # 科创50
)
"""默认保存成分快照的指数。"""

_ST_PREFIX = re.compile(r"^(?:\*|S\*?)?ST(?![A-Z])")
"""ST、*ST、SST、S*ST。后面跟英文字母的（如 STAR）不算。"""
_RECOVERABLE_REASONS = frozenset({"unsupported", "plugin_error"})
"""取成分时，这两种失败只影响这个指数；超时、崩溃、取消等照常让任务失败或重试。"""


class SnapshotUnavailableError(Exception):
    """这一项快照取不到。是“未知”，不写任何行；原因进任务结果，不让整个同步失败。"""


def is_st_name(name: str) -> bool:
    """按名称前缀判定 ST：去掉所有空白（含全角空格）、转大写后以 ST、*ST、SST、S*ST 开头。"""
    # NFKC 把全角的ＳＴ、＊换成半角
    folded = unicodedata.normalize("NFKC", name)
    return _ST_PREFIX.match("".join(folded.split()).upper()) is not None


def st_snapshot(securities: pl.DataFrame, day: date) -> tuple[pl.DataFrame, int]:
    """day 那天的 ST 名单：(date、symbol、name、status 四列, 状态未知的在市股票数)。

    只看在市股票：kind 为 stock，上市日不晚于 day（或未知），退市日为空或晚于 day。
    名称为空的 ST 状态未知：写成 status = "unknown"、name 为 null，不能当成非 ST。
    """
    listed = securities.filter(
        (pl.col("kind") == "stock")
        & (pl.col("list_date").is_null() | (pl.col("list_date") <= day))
        & (pl.col("delist_date").is_null() | (pl.col("delist_date") > day))
    )
    has_name = pl.col("name").is_not_null() & (pl.col("name").str.strip_chars() != "")
    named = listed.filter(has_name)
    unknown = listed.filter(~has_name)
    if listed.height and not named.height:
        raise SnapshotUnavailableError(f"{listed.height} 只在市股票的名称全部为空，无法判定 ST")
    st = named.filter(pl.col("name").map_elements(is_st_name, return_dtype=pl.Boolean())).select(
        pl.lit(day).alias("date"), "symbol", "name", pl.lit("st").alias("status")
    )
    unknown_rows = unknown.select(
        pl.lit(day).alias("date"),
        "symbol",
        pl.lit(None, dtype=pl.String()).alias("name"),
        pl.lit("unknown").alias("status"),
    )
    return pl.concat([st, unknown_rows]).sort("symbol"), unknown.height


def merge_st_rows(existing: pl.DataFrame, new: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """同一天重复同步：(要写入的行, 要作废的主键)。

    以这次为准，但这次状态未知（unknown）的股票，已有的行保持不变：之前知道的比现在不知道的
    好，也不能因为这次没拿到名称就把它从名单里删掉。
    """
    write = new.filter(
        ~(
            (pl.col("status") == "unknown")
            & pl.col("symbol").is_in(existing.get_column("symbol").to_list())
        )
    )
    gone = existing.filter(~pl.col("symbol").is_in(new.get_column("symbol").to_list()))
    return write, gone.select("date", "symbol")


def fetch_index_members(session: PluginSession, index: str, day: date) -> pl.DataFrame:
    """day 那天指数 index 的成分（date、index、symbol 三列）。

    取不到、格式不对、为空都抛 :class:`SnapshotUnavailableError`（插件没有实现、方法报错也是）。
    """
    try:
        df = session.call("index_constituents", index, day, day)
    except PluginRunError as exc:
        if exc.reason not in _RECOVERABLE_REASONS:
            raise
        what = "插件没有实现 index_constituents" if exc.reason == "unsupported" else exc.message
        raise SnapshotUnavailableError(what) from exc
    if not isinstance(df, pl.DataFrame):
        raise SnapshotUnavailableError(f"应返回 DataFrame，实际为 {type(df).__name__}")
    expected = {"symbol": pl.String(), "in_date": pl.Date(), "out_date": pl.Date()}
    problems = [
        f"列 {name} 应为 {dtype}，实际为 {df.schema.get(name)}"
        for name, dtype in expected.items()
        if df.schema.get(name) != dtype
    ]
    if problems:
        raise SnapshotUnavailableError("返回的格式不对：" + "；".join(problems))
    if df.get_column("symbol").null_count():
        raise SnapshotUnavailableError("成分里有空的证券代码")
    members = df.filter(
        (pl.col("in_date").is_null() | (pl.col("in_date") <= day))
        & (pl.col("out_date").is_null() | (pl.col("out_date") > day))
    )
    if not members.height:
        # 空名单是“没读到”，不是“指数没有成分”：不能写成空
        raise SnapshotUnavailableError(
            f"{day} 的成分为空（插件返回 {df.height} 行，没有在 {day} 生效的）"
        )
    return (
        members.select(pl.lit(day).alias("date"), pl.lit(index).alias("index"), "symbol")
        .unique()
        .sort("symbol")
    )


def stale_keys(existing: pl.DataFrame, new: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    """同一天重复同步时，已经入库但这次名单里没有的行的主键：要作废，以最后一次为准。"""
    return existing.select(keys).join(new.select(keys), on=keys, how="anti")


__all__ = [
    "DEFAULT_INDEXES",
    "SnapshotUnavailableError",
    "fetch_index_members",
    "is_st_name",
    "merge_st_rows",
    "st_snapshot",
    "stale_keys",
]
