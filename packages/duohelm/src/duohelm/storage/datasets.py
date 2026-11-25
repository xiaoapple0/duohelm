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

DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in (
        Dataset("bars_1d", BARS_SCHEMA, ("symbol", "date"), partition_by_year="date"),
        Dataset("adj_factors", ADJ_FACTORS_SCHEMA, ("symbol", "date")),
        Dataset("securities", SECURITIES_SCHEMA, ("symbol",)),
        Dataset("calendar", CALENDAR_SCHEMA, ("market", "date")),
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
