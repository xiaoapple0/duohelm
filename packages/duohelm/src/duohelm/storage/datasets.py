"""数据集定义：格式、主键、分区方式。

每个数据集的文件都带一个额外的 ``_deleted`` 列：为 True 的行是删除标记，表示这个
主键的数据作废（其余列为 null）。读取时按文件顺序合并，同一个主键以最后出现的为准，
最后去掉删除标记。
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

DELETED = "_deleted"


@dataclass(frozen=True)
class Dataset:
    name: str
    schema: dict[str, pl.DataType]
    keys: tuple[str, ...]
    partition_by_year: str | None = None
    """按这个日期列的年份分区；None 表示不分区。"""
    added_columns: tuple[str, ...] = ()
    """后来新增的可空列：旧文件没有这些列，读出来补为 null。其他列缺失照常报错。"""

    def __post_init__(self) -> None:
        # 分区由主键决定，同一主键的所有版本才会落在同一个分区里（整理时按分区合并）
        if self.partition_by_year is not None and self.partition_by_year not in self.keys:
            raise ValueError(f"{self.name}：分区列 {self.partition_by_year} 必须是主键的一部分")

    def partition_of(self, df: pl.DataFrame) -> pl.Series:
        """每一行所属的分区（字符串）。不分区的数据集统一为 "all"。"""
        if self.partition_by_year is None:
            return pl.Series("_partition", ["all"] * df.height, dtype=pl.String)
        return df.get_column(self.partition_by_year).dt.year().cast(pl.String).alias("_partition")

    def file_schema(self) -> dict[str, pl.DataType]:
        return {**self.schema, DELETED: pl.Boolean()}


CALENDAR_SCHEMA: dict[str, pl.DataType] = {"market": pl.String(), "date": pl.Date()}

DELIST_DATE_QUALITY_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "exact": pl.Boolean(),
    "source": pl.String(),
}
"""证券列表里每只证券的退市日是否精确（平台的数据集，不是插件的输出格式）。

``exact`` 为 True：精确（数据源没有声明是近似值）；为 False：数据源拿不到摘牌日，
``delist_date`` 是近似值（``Capabilities.exact_delist_date`` 为 False）；为 null：
仍在市，没有退市日。和 securities 在同一个事务里整行写入，两者一一对应。
快照里没有这个数据集的行时，是“不知道”，不是“精确”。``source`` 是这一行来自哪个数据源
插件（可为空，旧文件没有这一列，读出来为 null）：多个数据源写同一个快照时，靠它区分
“自己的行”和“别人的行”（tasks/delist_merge.py）。
"""

OBSERVED_AT = pl.Datetime("us", "Asia/Shanghai")
"""快照的拍摄时间（#54）：按 Asia/Shanghai 记录的带时区时间戳，可为空，不是主键。快照只能
用于拍摄时间之后的下一个交易日；没有这一列的旧文件，读出来为 null。"""

ST_DAILY_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "symbol": pl.String(),
    "name": pl.String(),
    "status": pl.String(),
    "observed_at": OBSERVED_AT,
}
"""每日 ST 名单快照（平台的数据集）：date 那天的 ST 证券和 ST 状态未知的证券。

``status`` 为 ``st``：名称是 ST 前缀；``unknown``：名称拿不到（``name`` 为 null），ST 状态
未知，不能当成非 ST。某个日期有任何一行才表示这一天有快照；有快照的日子里不在表里的在市
股票是非 ST；没有快照的日子是“不知道”，不是“没有 ST”。"""

INDEX_MEMBERS_DAILY_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "index": pl.String(),
    "symbol": pl.String(),
    "observed_at": OBSERVED_AT,
}
"""每日指数成分快照（平台的数据集）。某个日期某个指数有行才表示这一天有这个指数的快照。"""

QUALITY_REPORT_SCHEMA: dict[str, pl.DataType] = {"key": pl.String(), "report": pl.String()}
"""入库质量报告（平台的数据集，#31）：只有一行，``key`` 为 ``sync``，``report`` 是报告的 JSON。
和数据在同一个事务里写入，随快照保存；每次同步覆盖上一次的报告。快照里没有这一行时，表示
这个快照没有质量报告（早于 #31 的快照），不是“没有问题”。"""

DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in (
        Dataset("bars_1d", BARS_SCHEMA, ("symbol", "date"), partition_by_year="date"),
        Dataset("adj_factors", ADJ_FACTORS_SCHEMA, ("symbol", "date")),
        Dataset("securities", SECURITIES_SCHEMA, ("symbol",)),
        Dataset("calendar", CALENDAR_SCHEMA, ("market", "date")),
        Dataset(
            "delist_date_quality",
            DELIST_DATE_QUALITY_SCHEMA,
            ("symbol",),
            added_columns=("source",),
        ),
        Dataset("quality_report", QUALITY_REPORT_SCHEMA, ("key",)),
        Dataset(
            "st_daily",
            ST_DAILY_SCHEMA,
            ("date", "symbol"),
            partition_by_year="date",
            added_columns=("observed_at",),
        ),
        Dataset(
            "index_members_daily",
            INDEX_MEMBERS_DAILY_SCHEMA,
            ("date", "index", "symbol"),
            partition_by_year="date",
            added_columns=("observed_at",),
        ),
    )
}


class UnknownDatasetError(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(f"未知的数据集：{name}")


def get_dataset(name: str) -> Dataset:
    try:
        return DATASETS[name]
    except KeyError:
        raise UnknownDatasetError(name) from None
