"""数据来源记录（data_sources、source_config）和数据源配置表的存储（#99 的第 2 个 PR）。

只用 tmp_path，不访问网络。设计见 docs/subsystems/data-sources.md 的“来源记录”“可复现”“配置”。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from duohelm import endpoints as endpoints_module
from duohelm.data_source_config import DataSourceConfigError, DataSourceConfigStore
from duohelm.endpoints import EndpointStore
from duohelm.storage import DataStore, DataValidationError
from duohelm.storage.reader import MarketReader
from duohelm.storage.sources import (
    CategorySources,
    PluginRef,
    lookup_sources,
    read_source_config,
    write_source_config,
    write_sources,
)
from duohelm_sdk.schema import BARS_SCHEMA, SECURITIES_SCHEMA

A, B, C = "600000.SH", "000001.SZ", "000003.SZ"
D1, D2 = date(2023, 12, 29), date(2024, 1, 2)
XT = PluginRef("xtdata", "0.2.2")
ELT = PluginRef("eltdx", "0.3.0")


@pytest.fixture
def store(tmp_path: Path, clock) -> DataStore:
    return DataStore(tmp_path / "data", clock)


def old_snapshot(store: DataStore) -> str:
    """升级前的快照：只有行情数据集，没有来源记录。"""
    with store.transaction(note="旧版本的同步") as tx:
        tx.write(
            "securities",
            pl.DataFrame(
                [(s, "x", s[-2:], "stock", None, None) for s in (A, B)],
                schema=SECURITIES_SCHEMA,
                orient="row",
            ),
        )
        tx.write(
            "bars_1d",
            pl.DataFrame(
                [(d, A, 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0) for d in (D1, D2)],
                schema=BARS_SCHEMA,
                orient="row",
            ),
        )
        tx.write("calendar", pl.DataFrame({"market": ["CN", "CN"], "date": [D1, D2]}))
    assert tx.snapshot_id is not None
    return tx.snapshot_id


def file_hashes(root: Path) -> dict[str, str]:
    """数据根目录下所有数据文件和清单的 sha256（catalog.db 每次提交都会变，不算）。"""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith("catalog.db")
    }


# ---- 旧快照：来源未知 ----


def test_old_snapshot_reads_as_unknown_not_empty(store: DataStore) -> None:
    snap = old_snapshot(store)

    found = lookup_sources(store, "securities", [A, B])
    assert found.snapshot == snap
    assert not found.recorded
    # 每个请求的键都有一行，标明“来源未知”，不是空表
    assert found.items.get_column("key").to_list() == [A, B]
    assert found.items.get_column("known").to_list() == [False, False]
    assert found.items.get_column("source").to_list() == [None, None]
    assert found.unknown() == [A, B]
    # 快照里没有 source_config：不知道当时的配置，不是“没有配置”
    assert read_source_config(store) is None

    # 旧快照的行情照常读
    reader = MarketReader(store)
    assert reader.bars(A, snapshot=snap).bars.height == 2
    assert reader.securities(snapshot=snap).total == 2


def test_no_snapshot_at_all_is_unknown(store: DataStore) -> None:
    found = lookup_sources(store, "bars_1d", [A])
    assert found.snapshot is None
    assert not found.recorded
    assert found.unknown() == [A]
    assert read_source_config(store) is None


def test_keys_without_rows_are_unknown_even_with_config(store: DataStore) -> None:
    """有配置、有其他证券的来源时，没有行的证券仍是“来源未知”，不当成主数据源。"""
    old_snapshot(store)
    with store.transaction() as tx:
        write_sources(tx, "bars_1d", [A], source="xtdata", role="primary")
        write_source_config(tx, {"bars_1d": CategorySources(XT)})

    found = lookup_sources(store, "bars_1d", [A, B])
    assert found.recorded
    rows = {r["key"]: r for r in found.items.iter_rows(named=True)}
    assert rows[A] == {"key": A, "source": "xtdata", "role": "primary", "known": True}
    assert rows[B] == {"key": B, "source": None, "role": None, "known": False}
    # 别的类别没有记录，也是未知
    assert lookup_sources(store, "adj_factors", [A]).unknown() == [A]


# ---- 写入：重复写入不产生新快照 ----


def test_same_sources_written_again_creates_no_snapshot(store: DataStore) -> None:
    old_snapshot(store)
    config = {
        "securities": CategorySources(XT, ELT),
        "calendar": CategorySources(XT),
    }
    with store.transaction() as tx:
        assert write_sources(tx, "securities", [A, B], source="xtdata", role="primary") == 2
        assert write_sources(tx, "securities", [C], source="eltdx", role="backup") == 1
        assert write_sources(tx, "calendar", ["CN"], source="xtdata", role="primary") == 1
        assert write_source_config(tx, config) == 3
    first = tx.snapshot_id
    assert first is not None and store.head() == first

    with store.transaction() as tx:
        # 顺序不同、内容相同
        assert write_sources(tx, "securities", [B, A], source="xtdata", role="primary") == 0
        assert write_sources(tx, "securities", [C], source="eltdx", role="backup") == 0
        assert write_sources(tx, "calendar", ["CN"], source="xtdata", role="primary") == 0
        assert write_source_config(tx, config) == 0
    assert tx.snapshot_id is None
    assert store.head() == first


def test_changed_source_makes_new_snapshot_old_one_keeps_its_sources(store: DataStore) -> None:
    old_snapshot(store)
    with store.transaction() as tx:
        write_sources(tx, "adj_factors", [A, B], source="eltdx", role="primary")
    before = tx.snapshot_id
    assert before is not None

    with store.transaction() as tx:
        assert write_sources(tx, "adj_factors", [A], source="xtdata", role="primary") == 1
    after = tx.snapshot_id
    assert after is not None and after != before

    def sources(snap: str) -> dict[str, str]:
        items = lookup_sources(store, "adj_factors", [A, B], snapshot=snap).items
        return dict(zip(items.get_column("key"), items.get_column("source"), strict=True))

    assert sources(before) == {A: "eltdx", B: "eltdx"}
    assert sources(after) == {A: "xtdata", B: "eltdx"}


def test_source_config_roundtrip_and_backup_removal(store: DataStore) -> None:
    with store.transaction() as tx:
        write_source_config(
            tx, {"securities": CategorySources(XT, ELT), "bars_1d": CategorySources(ELT)}
        )
    with_backup = tx.snapshot_id
    assert with_backup is not None
    assert read_source_config(store) == {
        "securities": CategorySources(XT, ELT),
        "bars_1d": CategorySources(ELT),
    }

    # 去掉备用：backup 行删除，没有备用就没有 backup 行
    with store.transaction() as tx:
        assert write_source_config(tx, {"securities": CategorySources(XT)}) == 1
    assert read_source_config(store) == {
        "securities": CategorySources(XT),
        "bars_1d": CategorySources(ELT),
    }
    rows = store.read("source_config")
    assert rows.filter(pl.col("role") == "backup").height == 0
    # 写入时的配置随快照固定
    assert read_source_config(store, snapshot=with_backup) == {
        "securities": CategorySources(XT, ELT),
        "bars_1d": CategorySources(ELT),
    }

    # 只升级插件版本：产生一个新快照
    with store.transaction() as tx:
        write_source_config(tx, {"securities": CategorySources(PluginRef("xtdata", "0.2.3"))})
    assert tx.snapshot_id is not None


def test_unknown_category_in_snapshot_is_skipped(
    store: DataStore, caplog: pytest.LogCaptureFixture
) -> None:
    """以后的版本可能新增类别：读到不认识的 id 跳过并记日志，不报错。"""
    with store.transaction() as tx:
        write_source_config(tx, {"calendar": CategorySources(XT)})
        tx.write(
            "source_config",
            pl.DataFrame(
                {
                    "category": ["bars_1m", "calendar"],
                    "role": ["primary", "standby"],
                    "plugin_id": ["xtdata", "eltdx"],
                    "plugin_version": ["1", "1"],
                }
            ),
        )
    assert read_source_config(store) == {"calendar": CategorySources(XT)}
    assert "bars_1m" in caplog.text


@pytest.mark.parametrize(
    ("category", "source", "role", "keys"),
    [
        ("bars_1m", "xtdata", "primary", [A]),
        ("bars_1d", "", "primary", [A]),
        ("bars_1d", "xtdata", "standby", [A]),
        ("bars_1d", "xtdata", "primary", [""]),
        ("bars_1d", "xtdata", "primary", [A, A]),
    ],
)
def test_bad_source_rows_rejected(
    store: DataStore, category: str, source: str, role: str, keys: list[str]
) -> None:
    with pytest.raises(DataValidationError), store.transaction() as tx:
        write_sources(tx, category, keys, source=source, role=role)  # type: ignore[arg-type]
    assert store.head() is None


def test_backup_same_as_primary_rejected(store: DataStore) -> None:
    with pytest.raises(DataValidationError), store.transaction() as tx:
        write_source_config(tx, {"bars_1d": CategorySources(XT, PluginRef("xtdata", "0.1"))})
    assert store.head() is None


# ---- 迁移：只新增，不改已有数据 ----


def v1_plugin_config(path: Path) -> None:
    """升级前（v1）的 plugin-config.db，带一行已确认的地址和一条审计。"""
    conn = sqlite3.connect(path)
    for statement in endpoints_module._SCHEMA_V1.split(";"):  # pyright: ignore[reportPrivateUsage]
        if statement.strip():
            conn.execute(statement)
    conn.execute("INSERT INTO plugin_endpoints VALUES ('xtdata', 'svc', '127.0.0.1:58610', 'd', 7)")
    conn.execute("INSERT INTO plugin_network VALUES ('eltdx', '[\"a.example:7709\"]', 'n', 8)")
    conn.execute(
        "INSERT INTO permission_audit VALUES (1, 'xtdata', 'endpoint:svc', 'confirm', NULL, "
        "'127.0.0.1:58610', 'd', 7)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = sqlite3.connect(path)
    try:
        return {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            for t in ("plugin_endpoints", "plugin_network", "permission_audit")
        }
    finally:
        conn.close()


def test_upgrade_keeps_existing_data_files_and_config(tmp_path: Path, store: DataStore) -> None:
    snap = old_snapshot(store)
    old_manifest = store.manifest(snap)
    old_bars = store.read("bars_1d", snapshot=snap)
    before_files = file_hashes(store.root)
    assert any(k.endswith(".parquet") for k in before_files)
    config_path = tmp_path / "plugin-config.db"
    v1_plugin_config(config_path)
    before_rows = dump(config_path)

    # 迁移 plugin-config.db；两个入口都能触发，顺序无关
    configs = DataSourceConfigStore(config_path)
    EndpointStore(config_path)
    conn = sqlite3.connect(config_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
    assert dump(config_path) == before_rows
    assert configs.get() == {}  # 升级后没有配置：同步按现在的单数据源行为

    # 升级后第一次写入来源记录
    with store.transaction() as tx:
        write_sources(tx, "bars_1d", [A], source="xtdata", role="primary")
        write_source_config(tx, {"bars_1d": CategorySources(XT)})
    assert tx.snapshot_id is not None

    after_files = file_hashes(store.root)
    # 已有的每个数据文件和清单都还在，sha256 不变；只新增了来源记录的文件和新清单
    assert {k: after_files.get(k) for k in before_files} == before_files
    added = set(after_files) - set(before_files)
    assert added and all(
        k.startswith(("files/data_sources/", "files/source_config/", "manifests/")) for k in added
    )
    # 新快照里已有数据集的文件列表原样沿用，旧快照照常读出同样的数据
    new_manifest = store.manifest(tx.snapshot_id)
    for name, entries in old_manifest.datasets.items():
        assert new_manifest.datasets[name] == entries
    assert store.read("bars_1d", snapshot=snap).equals(old_bars)
    assert store.read("bars_1d").equals(old_bars)


def test_fresh_plugin_config_has_both_versions(tmp_path: Path) -> None:
    path = tmp_path / "plugin-config.db"
    DataSourceConfigStore(path)
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
    assert {
        "plugin_endpoints",
        "plugin_network",
        "permission_audit",
        "data_source_config",
    } <= tables


def test_newer_plugin_config_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plugin-config.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="比当前平台支持的 2 新"):
        DataSourceConfigStore(path)


# ---- 数据源配置表 ----


def test_config_put_get_and_clear(tmp_path: Path, clock) -> None:
    path = tmp_path / "plugin-config.db"
    configs = DataSourceConfigStore(path, clock)

    saved = configs.put({"securities": ("xtdata", "eltdx"), "bars_1d": ("eltdx", None)})
    assert {c: (v.primary, v.backup) for c, v in saved.items()} == {
        "securities": ("xtdata", "eltdx"),
        "bars_1d": ("eltdx", None),
    }
    first_at = saved["securities"].updated_at

    # 整体替换：没给的类别删除；内容没变的类别不改 updated_at
    clock.advance(1000)
    saved = DataSourceConfigStore(path, clock).put({"securities": ("xtdata", "eltdx")})
    assert list(saved) == ["securities"]
    assert saved["securities"].updated_at == first_at
    saved = configs.put({"securities": ("xtdata", None)})
    assert saved["securities"].backup is None
    assert saved["securities"].updated_at > first_at

    assert configs.put({}) == {}
    assert configs.get() == {}


@pytest.mark.parametrize(
    "config",
    [
        {"bars_1m": ("xtdata", None)},
        {"bars_1d": ("", None)},
        {"bars_1d": ("xtdata", "xtdata")},
        {"bars_1d": ("xtdata", "")},
    ],
)
def test_config_rejects_invalid(tmp_path: Path, config: dict[str, tuple[str, str | None]]) -> None:
    configs = DataSourceConfigStore(tmp_path / "plugin-config.db")
    configs.put({"calendar": ("xtdata", None)})
    with pytest.raises(DataSourceConfigError):
        configs.put(config)
    # 校验失败不改已有配置
    assert {c: v.primary for c, v in configs.get().items()} == {"calendar": "xtdata"}
