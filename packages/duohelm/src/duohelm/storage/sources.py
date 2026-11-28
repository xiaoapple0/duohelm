"""数据来源记录：``data_sources`` 和 ``source_config`` 两个数据集的写入和读取（#99）。

设计见 docs/subsystems/data-sources.md 的“来源记录”“可复现”。要点：

- 来源随快照保存，没有时间戳：同样的来源写两次，``Transaction.write`` 不写任何行，不产生新快照
- **没有行就是“来源未知”**，不能当成主数据源，也不按快照的 note、任务 id 猜。升级前的快照没有
  这两个数据集，读出来全部是“来源未知”（``SourceLookup.recorded`` 为 False），不是空表
- 只新增数据集，不改写、不删除任何已入库的数据
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

import polars as pl

from duohelm.storage.store import DataStore, DataValidationError, Transaction

logger = logging.getLogger(__name__)

CATEGORIES: tuple[str, ...] = ("securities", "calendar", "bars_1d", "adj_factors")
"""数据类别 id（data-sources.md 的“数据类别”）。以后可能新增：读到不认识的 id 跳过并记日志。"""

SourceRole = Literal["primary", "backup"]
ROLES: tuple[SourceRole, ...] = ("primary", "backup")

DATA_SOURCES = "data_sources"
SOURCE_CONFIG = "source_config"


@dataclass(frozen=True)
class PluginRef:
    id: str
    version: str


@dataclass(frozen=True)
class CategorySources:
    """一个类别写入时的主、备数据源。"""

    primary: PluginRef
    backup: PluginRef | None = None


@dataclass(frozen=True)
class SourceLookup:
    """一个类别里一批键（证券代码或市场）的来源。"""

    snapshot: str | None
    recorded: bool
    """快照里有没有 ``data_sources`` 数据集。False（升级前的快照、还没有快照）时
    所有键都是“来源未知”。"""
    items: pl.DataFrame
    """每个请求的键一行，按请求的顺序：key、source、role、known。``known`` 为 False 时来源未知，
    ``source``、``role`` 为 null。"""

    def unknown(self) -> list[str]:
        return self.items.filter(~pl.col("known")).get_column("key").to_list()


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise DataValidationError(f"未知的数据类别：{category!r}，应为 {list(CATEGORIES)} 之一")


def _check_plugin(plugin_id: str, what: str) -> None:
    if not plugin_id:
        raise DataValidationError(f"{what}的插件 id 不能为空")


def source_rows(
    category: str, keys: Iterable[str], *, source: str, role: SourceRole
) -> pl.DataFrame:
    """``data_sources`` 的行：这些键在这个类别里的来源都是 source（角色 role）。"""
    _check_category(category)
    _check_plugin(source, "来源")
    if role not in ROLES:
        raise DataValidationError(f"未知的角色：{role!r}，应为 {list(ROLES)} 之一")
    key = pl.Series("key", list(keys), dtype=pl.String)
    if (key.is_null() | (key == "")).any():
        raise DataValidationError("来源记录的键不能为空")
    return pl.DataFrame(key).select(
        pl.lit(category, dtype=pl.String).alias("category"),
        pl.col("key"),
        pl.lit(source, dtype=pl.String).alias("source"),
        pl.lit(role, dtype=pl.String).alias("role"),
    )


def write_sources(
    tx: Transaction, category: str, keys: Iterable[str], *, source: str, role: SourceRole
) -> int:
    """记录这些键的来源。只写来源有变化的行，返回写入的行数（全都没变时为 0）。"""
    return tx.write(DATA_SOURCES, source_rows(category, keys, source=source, role=role))


def write_source_config(tx: Transaction, config: Mapping[str, CategorySources]) -> int:
    """记录写入这个快照时 config 里各类别的主、备数据源；没有备用的类别去掉已有的 backup 行。

    config 里没有的类别不动。配置和插件版本都没变时不写任何行。返回写入和删除的行数。
    """
    rows: list[tuple[str, str, str, str]] = []
    no_backup: list[str] = []
    for category, sources in config.items():
        _check_category(category)
        _check_plugin(sources.primary.id, f"{category} 主数据源")
        rows.append((category, "primary", sources.primary.id, sources.primary.version))
        if sources.backup is None:
            no_backup.append(category)
            continue
        _check_plugin(sources.backup.id, f"{category} 备用数据源")
        if sources.backup.id == sources.primary.id:
            raise DataValidationError(f"{category} 的备用数据源不能和主数据源是同一个插件")
        rows.append((category, "backup", sources.backup.id, sources.backup.version))
    schema = {"category": pl.String, "role": pl.String, "plugin_id": pl.String}
    written = tx.write(
        SOURCE_CONFIG,
        pl.DataFrame(rows, schema={**schema, "plugin_version": pl.String}, orient="row"),
    )
    stale = pl.DataFrame(
        {"category": no_backup, "role": ["backup"] * len(no_backup)},
        schema={"category": pl.String, "role": pl.String},
    )
    return written + (tx.delete(SOURCE_CONFIG, stale) if stale.height else 0)


def lookup_sources(
    store: DataStore, category: str, keys: Iterable[str], *, snapshot: str | None = None
) -> SourceLookup:
    """快照（默认当前快照）里这些键在这个类别的来源。没有记录的键是“来源未知”。"""
    _check_category(category)
    wanted = pl.DataFrame({"key": list(keys)}, schema={"key": pl.String}).unique(
        maintain_order=True
    )
    snap = snapshot if snapshot is not None else store.head()
    recorded = bool(store.manifest(snap).datasets.get(DATA_SOURCES))
    rows = (
        store.read(DATA_SOURCES, snapshot=snap)
        .filter(pl.col("category") == category)
        .select("key", "source", "role")
    )
    # 不认识的角色（以后的版本可能新增）跳过并记日志：这一行读不懂，键按“来源未知”处理，
    # 不能当成已知来源
    odd = rows.filter(~pl.col("role").is_in(ROLES) | pl.col("role").is_null())
    if odd.height:
        logger.warning(
            "快照 %s 的 data_sources 里 %s 类别有 %d 行角色不认识（%s），按来源未知处理",
            snap,
            category,
            odd.height,
            ", ".join(sorted({str(r) for r in odd.get_column("role").to_list()})),
        )
        rows = rows.join(odd.select("key"), on="key", how="anti")
    # 左连接：没有行的键 source 为 null，即“来源未知”；不按配置或别的线索补
    items = wanted.join(rows, on="key", how="left", maintain_order="left").with_columns(
        pl.col("source").is_not_null().alias("known")
    )
    return SourceLookup(snap, recorded, items)


def read_source_config(
    store: DataStore, *, snapshot: str | None = None
) -> dict[str, CategorySources] | None:
    """快照（默认当前快照）写入时的数据源配置。快照里没有 ``source_config`` 时返回 None（不知道）。

    不认识的类别和角色跳过并记日志；有 backup 行却没有 primary 行的类别也跳过并记日志。
    """
    snap = snapshot if snapshot is not None else store.head()
    if not store.manifest(snap).datasets.get(SOURCE_CONFIG):
        return None
    primary: dict[str, PluginRef] = {}
    backup: dict[str, PluginRef] = {}
    for row in store.read(SOURCE_CONFIG, snapshot=snap).iter_rows(named=True):
        category, role = row["category"], row["role"]
        if category not in CATEGORIES or role not in ROLES:
            logger.warning(
                "快照 %s 的 source_config 有不认识的类别或角色：%s/%s", snap, category, role
            )
            continue
        ref = PluginRef(row["plugin_id"], row["plugin_version"])
        (primary if role == "primary" else backup)[category] = ref
    for category in sorted(set(backup) - set(primary)):
        logger.warning(
            "快照 %s 的 source_config 里 %s 只有备用、没有主数据源，跳过", snap, category
        )
    return {c: CategorySources(p, backup.get(c)) for c, p in primary.items()}
