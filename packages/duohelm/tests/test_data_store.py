"""行情存储与快照的测试。只用 tmp_path，不访问网络。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from duohelm.storage import (
    CorruptFileError,
    Dataset,
    DataStore,
    DataValidationError,
    SnapshotConflictError,
    SnapshotNotFoundError,
)
from duohelm.storage import catalog as catalog_module
from duohelm.storage.datasets import UnknownDatasetError
from duohelm_sdk.schema import BARS_SCHEMA, SECURITIES_SCHEMA


def bars(rows: list[tuple[date, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        [(d, s, c, c, c, c, 100.0, c * 100) for d, s, c in rows],
        schema=BARS_SCHEMA,
        orient="row",
    )


def secs(rows: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        [(s, n, s[-2:], "stock", None, None) for s, n in rows],
        schema=SECURITIES_SCHEMA,
        orient="row",
    )


def keys(rows: list[tuple[date, str]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema={"date": pl.Date, "symbol": pl.String}, orient="row")


D1, D2, D3 = date(2023, 12, 29), date(2024, 1, 2), date(2024, 1, 3)
A, B = "600000.SH", "000001.SZ"


@pytest.fixture
def store(tmp_path: Path, clock) -> DataStore:
    return DataStore(tmp_path / "data", clock)


def write(store: DataStore, dataset: str, df: pl.DataFrame) -> str | None:
    with store.transaction() as tx:
        tx.write(dataset, df)
    return tx.snapshot_id


def closes(store: DataStore, **kw: object) -> dict[tuple[date, str], float]:
    df = store.read("bars_1d", **kw)  # type: ignore[arg-type]
    return {(r["date"], r["symbol"]): r["close"] for r in df.iter_rows(named=True)}


# ---- 写入与读取 ----


def test_empty(store: DataStore) -> None:
    assert store.head() is None
    df = store.read("bars_1d")
    assert df.height == 0
    assert df.schema == BARS_SCHEMA


def test_roundtrip(store: DataStore) -> None:
    sid = write(store, "bars_1d", bars([(D2, A, 10.0), (D1, A, 9.0), (D2, B, 5.0)]))
    assert sid is not None
    assert store.head() == sid
    out = store.read("bars_1d")
    assert out.schema == BARS_SCHEMA
    assert out.select("symbol", "date").rows() == [(B, D2), (A, D1), (A, D2)]  # 按主键排序


def test_later_write_wins(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0), (D3, A, 11.0)]))
    write(store, "bars_1d", bars([(D2, A, 10.5)]))  # 修正
    assert closes(store) == {(D2, A): 10.5, (D3, A): 11.0}


def test_unchanged_write_creates_no_snapshot(store: DataStore) -> None:
    first = write(store, "bars_1d", bars([(D2, A, 10.0), (D3, A, 11.0)]))
    files_before = sorted(p.name for p in store.files_dir.rglob("*.parquet"))
    with store.transaction() as tx:
        n = tx.write("bars_1d", bars([(D2, A, 10.0), (D3, A, 11.0)]))
    assert n == 0
    assert tx.snapshot_id is None
    assert store.head() == first
    assert sorted(p.name for p in store.files_dir.rglob("*.parquet")) == files_before


def test_only_changed_rows_written(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0), (D3, A, 11.0)]))
    with store.transaction() as tx:
        n = tx.write("bars_1d", bars([(D2, A, 10.0), (D3, A, 11.5), (D3, B, 5.0)]))
    assert n == 2
    assert store.catalog.snapshot(tx.snapshot_id or "").stats == {"bars_1d": {"written": 2}}


def test_filters_and_partition_pruning(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D1, A, 9.0), (D2, A, 10.0), (D2, B, 5.0), (D3, B, 6.0)]))
    assert set(closes(store, start=D2)) == {(D2, A), (D2, B), (D3, B)}
    assert set(closes(store, end=D1)) == {(D1, A)}
    assert set(closes(store, symbols=[B], start=D3)) == {(D3, B)}
    partitions = {p.parent.name for p in store.files_dir.rglob("*.parquet")}
    assert partitions == {"2023", "2024"}


# ---- 删除标记 ----


def test_delete_marks_key_void(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0), (D3, A, 11.0)]))
    with store.transaction() as tx:
        assert tx.delete("bars_1d", keys([(D2, A)])) == 1
    assert closes(store) == {(D3, A): 11.0}


def test_rewrite_after_delete(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0)]))
    with store.transaction() as tx:
        tx.delete("bars_1d", keys([(D2, A)]))
    write(store, "bars_1d", bars([(D2, A, 10.2)]))
    assert closes(store) == {(D2, A): 10.2}


def test_delete_missing_key_is_noop(store: DataStore) -> None:
    first = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    with store.transaction() as tx:
        assert tx.delete("bars_1d", keys([(D3, A), (D2, B)])) == 0
    assert tx.snapshot_id is None
    assert store.head() == first


def test_delete_needs_keys(store: DataStore) -> None:
    with store.transaction() as tx, pytest.raises(DataValidationError):
        tx.delete("bars_1d", pl.DataFrame({"symbol": [A]}))


# ---- 快照 ----


def test_old_snapshot_still_readable(store: DataStore) -> None:
    first = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    write(store, "bars_1d", bars([(D2, A, 10.5), (D3, A, 11.0)]))
    with store.transaction() as tx:
        tx.delete("bars_1d", keys([(D3, A)]))
    assert closes(store, snapshot=first) == {(D2, A): 10.0}
    assert closes(store) == {(D2, A): 10.5}


def test_snapshot_id_is_manifest_hash(store: DataStore) -> None:
    sid = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    assert sid is not None
    manifest = store.manifest(sid)
    assert manifest.snapshot_id == sid
    raw = (store.manifests_dir / f"{sid}.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sid


def test_same_writes_same_snapshot_id(tmp_path: Path, clock) -> None:
    """快照 id 只由内容决定：不同目录、数据集写入顺序不同，结果也相同。"""
    a = DataStore(tmp_path / "a", clock)
    b = DataStore(tmp_path / "b", clock)
    with a.transaction() as tx:
        tx.write("bars_1d", bars([(D2, A, 10.0)]))
        tx.write("securities", secs([(A, "浦发银行")]))
    with b.transaction() as tx:
        tx.write("securities", secs([(A, "浦发银行")]))
        tx.write("bars_1d", bars([(D2, A, 10.0)]))
    assert a.head() == b.head()


def test_conflicting_commit_rejected(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0)]))
    first = store.transaction()
    second = store.transaction()
    first.write("bars_1d", bars([(D3, A, 11.0)]))
    first.commit()
    second.write("bars_1d", bars([(D3, B, 5.0)]))
    new_files = set(store.files_dir.rglob("*.parquet"))
    with pytest.raises(SnapshotConflictError):
        second.commit()
    assert closes(store) == {(D2, A): 10.0, (D3, A): 11.0}
    assert len(set(store.files_dir.rglob("*.parquet"))) == len(new_files) - 1  # 冲突方的文件已删


def test_error_inside_transaction_aborts(store: DataStore) -> None:
    first = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    files_before = set(store.files_dir.rglob("*.parquet"))
    with pytest.raises(RuntimeError), store.transaction() as tx:
        tx.write("bars_1d", bars([(D3, A, 11.0)]))
        raise RuntimeError("同步中途失败")
    assert store.head() == first
    assert set(store.files_dir.rglob("*.parquet")) == files_before


def test_abort_keeps_reused_files(store: DataStore) -> None:
    """内容相同的文件会被复用；中止时不能删掉被旧快照引用的文件。"""
    first = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    with store.transaction() as tx:
        tx.delete("bars_1d", keys([(D2, A)]))
    with pytest.raises(RuntimeError), store.transaction() as tx:
        tx.write("bars_1d", bars([(D2, A, 10.0)]))  # 内容与第一个文件相同
        raise RuntimeError("中止")
    assert closes(store, snapshot=first, verify=True) == {(D2, A): 10.0}


def test_verify_detects_corruption(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0)]))
    [path] = list(store.files_dir.rglob("*.parquet"))
    path.write_bytes(path.read_bytes()[:-8] + b"tampered")
    with pytest.raises(CorruptFileError):
        store.read("bars_1d", verify=True)


def test_tampered_manifest_rejected(store: DataStore) -> None:
    sid = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    path = store.manifests_dir / f"{sid}.json"
    path.write_bytes(path.read_bytes().replace(b'"rows":1', b'"rows":2'))
    with pytest.raises(CorruptFileError):
        store.read("bars_1d")


# ---- 整理 ----


def test_compact_preserves_data(store: DataStore) -> None:
    write(store, "bars_1d", bars([(D1, A, 9.0), (D2, A, 10.0), (D3, A, 11.0)]))
    write(store, "bars_1d", bars([(D2, A, 10.5), (D2, B, 5.0)]))
    with store.transaction() as tx:
        tx.delete("bars_1d", keys([(D3, A)]))
    before_id = store.head()
    before = store.read("bars_1d")
    sid = store.compact("bars_1d")
    assert sid is not None
    assert store.read("bars_1d").equals(before)
    files = store.manifest(sid).datasets["bars_1d"]
    assert sorted(e.partition for e in files) == ["2023", "2024"]  # 每个分区一个文件
    raw = pl.concat([pl.read_parquet(store.files_dir / e.path) for e in files])
    assert not raw.get_column("_deleted").any()  # 删除标记已经消化
    assert closes(store, snapshot=before_id) == closes(store)  # 旧快照照样能读
    assert store.compact("bars_1d") is None  # 已经整理过


# ---- 清理 ----


def test_gc_keeps_head_pinned_and_recent(store: DataStore, clock) -> None:
    old = write(store, "bars_1d", bars([(D2, A, 10.0)]))
    pinned = write(store, "bars_1d", bars([(D2, A, 10.1)]))
    assert old is not None and pinned is not None
    store.catalog.pin(pinned, "backtest:1")
    collectable = write(store, "bars_1d", bars([(D2, A, 10.2)]))
    head = write(store, "bars_1d", bars([(D2, A, 10.3)]))
    assert collectable is not None
    clock.advance(40 * 86400 * 1000)
    _age_all_files(store, days=40)
    report = store.gc(keep_days=30, min_file_age_s=3600)
    assert report.collected_snapshots == 2  # old 和 collectable
    assert closes(store, snapshot=pinned, verify=True) == {(D2, A): 10.1}
    assert closes(store, snapshot=head, verify=True) == {(D2, A): 10.3}
    for sid in (old, collectable):
        with pytest.raises(SnapshotNotFoundError):
            store.read("bars_1d", snapshot=sid)
        with pytest.raises(SnapshotNotFoundError):
            store.catalog.pin(sid, "late")


def test_gc_spares_young_orphans(store: DataStore, clock) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0)]))
    tx = store.transaction()
    tx.write("bars_1d", bars([(D3, A, 11.0)]))  # 写了文件但还没提交
    report = store.gc(keep_days=0, min_file_age_s=3600)
    assert report.deleted_files == 0
    tx.commit()
    assert closes(store, verify=True) == {(D2, A): 10.0, (D3, A): 11.0}


def test_gc_deletes_old_orphans(store: DataStore, clock) -> None:
    write(store, "bars_1d", bars([(D2, A, 10.0)]))
    orphan = store.files_dir / "bars_1d" / "2024" / "leftover.parquet"
    orphan.write_bytes(b"x")
    _age(orphan, days=2)
    clock.advance(2 * 86400 * 1000)
    store.gc(keep_days=30, min_file_age_s=3600)
    assert not orphan.exists()
    assert closes(store, verify=True) == {(D2, A): 10.0}


def _age(path: Path, *, days: float) -> None:
    t = path.stat().st_mtime - days * 86400
    os.utime(path, (t, t))


def _age_all_files(store: DataStore, *, days: float) -> None:
    for path in store.files_dir.rglob("*.parquet"):
        _age(path, days=days)


# ---- 校验 ----


def test_rejects_bad_frames(store: DataStore) -> None:
    with store.transaction() as tx:
        with pytest.raises(DataValidationError, match="列应为"):
            tx.write("bars_1d", pl.DataFrame({"symbol": [A]}))
        with pytest.raises(DataValidationError, match="重复"):
            tx.write("bars_1d", bars([(D2, A, 10.0), (D2, A, 10.0)]))
        with pytest.raises(UnknownDatasetError):
            tx.write("nope", bars([]))
    assert store.head() is None


def test_partition_column_must_be_key() -> None:
    with pytest.raises(ValueError, match="主键"):
        Dataset("x", BARS_SCHEMA, ("symbol",), partition_by_year="date")


# ---- 退市股扫描记录 ----


def test_scan_registry(store: DataStore) -> None:
    catalog = store.catalog
    assert catalog.unscanned("a", ["600001.SH", "600002.SH", "600003.SH"]) == [
        "600001.SH",
        "600002.SH",
        "600003.SH",
    ]
    catalog.record_scan("a", [("600001.SH", True), ("600002.SH", False)])
    assert catalog.unscanned("a", ["600001.SH", "600002.SH", "600003.SH"]) == ["600003.SH"]
    assert catalog.scan_summary("a") == {"scanned": 2, "found": 1}
    assert catalog.found_symbols("a") == ["600001.SH"]
    # 扫描记录与快照无关：重新打开也还在
    again = DataStore(store.root)
    assert again.catalog.unscanned("a", ["600001.SH", "600003.SH"]) == ["600003.SH"]


def test_scan_registry_is_per_source(store: DataStore) -> None:
    """一个数据源没有某个代码，不代表别的数据源也没有。"""
    catalog = store.catalog
    catalog.record_scan("a", [("600001.SH", False)])
    assert catalog.unscanned("b", ["600001.SH"]) == ["600001.SH"]
    assert catalog.scan_summary("b") == {"scanned": 0, "found": 0}


def test_scan_found_is_never_cleared(store: DataStore) -> None:
    """扫到过的代码不会被后来的“没找到”改掉：之后每次同步都要重新查询它。"""
    catalog = store.catalog
    catalog.record_scan("a", [("600001.SH", True)])
    catalog.record_scan("a", [("600001.SH", False)])
    assert catalog.found_symbols("a") == ["600001.SH"]


def test_catalog_v1_migrates_and_keeps_old_scan_table(tmp_path: Path) -> None:
    """v1 的目录库升级到 v2：旧的扫描表改名保留，不删除。"""
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    for statement in catalog_module._SCHEMA_V1.split(";"):  # pyright: ignore[reportPrivateUsage]
        if statement.strip():
            conn.execute(statement)
    conn.execute("INSERT INTO security_scan VALUES ('600001.SH', 1, 0)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    catalog = catalog_module.Catalog(path)
    assert catalog.unscanned("a", ["600001.SH"]) == ["600001.SH"]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT symbol FROM security_scan_v1").fetchall() == [("600001.SH",)]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    conn.close()
