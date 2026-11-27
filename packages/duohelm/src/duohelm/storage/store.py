"""行情存储：只新增文件的 Parquet 数据集，用清单记录生效的文件，清单的哈希即快照 id。

设计见 docs/subsystems/data.md 的“存储”一节。要点：

- 数据文件写好后从不修改。修改和删除都通过追加新文件完成：后面的文件覆盖前面的
  同主键行；``_deleted`` 为 True 的行是删除标记
- 清单是规范化的 JSON，列出每个数据集生效的文件（顺序即生效顺序）及其 sha256。
  清单的 sha256 就是快照 id：内容相同，id 相同
- 写入前和当前快照比对，只写入新增或变化的行，所以重复写入同样的数据不会产生新快照
- 切换当前快照用乐观锁：提交时当前快照必须仍是写入开始时的快照
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import polars as pl

from duohelm.storage.catalog import Catalog, SnapshotNotFoundError
from duohelm.storage.datasets import DELETED, Dataset, get_dataset
from duohelm.tasks.store import Clock, now_ms
from duohelm_sdk.schema import schema_problems

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
_SEQ = "_seq"
_PARTITION = "_partition"


class DataValidationError(ValueError):
    pass


class CorruptFileError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileEntry:
    path: str
    """相对 files/ 目录的路径，用 / 分隔。"""
    sha256: str
    rows: int
    partition: str

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "rows": self.rows,
            "partition": self.partition,
        }


@dataclass(frozen=True)
class Manifest:
    datasets: dict[str, tuple[FileEntry, ...]] = field(default_factory=lambda: {})

    def canonical(self) -> bytes:
        body = {
            "version": MANIFEST_VERSION,
            "datasets": {
                name: [e.to_json() for e in entries]
                for name, entries in sorted(self.datasets.items())
                if entries
            },
        }
        return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    @classmethod
    def parse(cls, raw: bytes) -> Manifest:
        body = json.loads(raw)
        if body.get("version") != MANIFEST_VERSION:
            raise CorruptFileError(f"不支持的清单版本：{body.get('version')}")
        return cls(
            {
                name: tuple(FileEntry(**e) for e in entries)
                for name, entries in body["datasets"].items()
            }
        )

    def files(self) -> Iterator[FileEntry]:
        for entries in self.datasets.values():
            yield from entries


@dataclass(frozen=True)
class GcReport:
    collected_snapshots: int
    deleted_files: int
    kept_files: int


class DataStore:
    """数据根目录（通常是 $DUOHELM_HOME/data）下的全部行情数据。"""

    def __init__(self, root: Path, clock: Clock = now_ms) -> None:
        self.root = root
        self.files_dir = root / "files"
        self.manifests_dir = root / "manifests"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(root / "catalog.db", clock)
        self._clock = clock

    # ---- 快照 ----

    def head(self) -> str | None:
        return self.catalog.head()

    def manifest(self, snapshot_id: str | None) -> Manifest:
        if snapshot_id is None:
            return Manifest()
        path = self.manifests_dir / f"{snapshot_id}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise SnapshotNotFoundError(snapshot_id) from None
        if hashlib.sha256(raw).hexdigest() != snapshot_id:
            raise CorruptFileError(f"清单 {snapshot_id} 的内容与 id 不符")
        return Manifest.parse(raw)

    def transaction(self, *, task_id: int | None = None, note: str | None = None) -> Transaction:
        return Transaction(self, task_id=task_id, note=note)

    # ---- 读 ----

    def read(
        self,
        dataset: str,
        *,
        snapshot: str | None = None,
        symbols: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        columns: Sequence[str] | None = None,
        verify: bool = False,
    ) -> pl.DataFrame:
        """读出某个快照（默认当前快照）里一个数据集的生效数据，按主键排序。

        symbols / start / end 只对含 symbol / date 列的数据集生效。columns 只返回这些列
        （覆盖统计这类大范围读取只取需要的列）。verify=True 时先校验每个文件的 sha256。
        """
        ds = get_dataset(dataset)
        snap = snapshot if snapshot is not None else self.head()
        entries = list(enumerate(self.manifest(snap).datasets.get(dataset, ())))
        if ds.partition_by_year is not None and (start or end):
            lo = start.year if start else -1
            hi = end.year if end else 10_000
            entries = [(i, e) for i, e in entries if lo <= int(e.partition) <= hi]
        return self._resolve(
            ds, entries, symbols=symbols, start=start, end=end, columns=columns, verify=verify
        )

    def _resolve(
        self,
        ds: Dataset,
        entries: Sequence[tuple[int, FileEntry]],
        *,
        symbols: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        columns: Sequence[str] | None = None,
        verify: bool = False,
        keep_deleted: bool = False,
    ) -> pl.DataFrame:
        if not entries:
            empty = pl.DataFrame(schema=ds.file_schema() if keep_deleted else ds.schema)
            return empty if columns is None else empty.select(columns)
        frames: list[pl.LazyFrame] = []
        for seq, entry in entries:
            path = self.files_dir / entry.path
            if verify and _sha256(path) != entry.sha256:
                raise CorruptFileError(f"数据文件 {entry.path} 的内容与清单不符")
            frames.append(pl.scan_parquet(path).with_columns(pl.lit(seq).alias(_SEQ)))
        lf = pl.concat(frames, how="vertical")
        if symbols is not None and "symbol" in ds.schema:
            lf = lf.filter(pl.col("symbol").is_in(list(symbols)))
        if start is not None and "date" in ds.schema:
            lf = lf.filter(pl.col("date") >= start)
        if end is not None and "date" in ds.schema:
            lf = lf.filter(pl.col("date") <= end)
        keys = list(ds.keys)
        lf = lf.sort([*keys, _SEQ]).unique(subset=keys, keep="last", maintain_order=True)
        if not keep_deleted:
            lf = lf.filter(~pl.col(DELETED)).drop(DELETED)
        lf = lf.drop(_SEQ).sort(keys)
        return (lf if columns is None else lf.select(columns)).collect()

    # ---- 维护 ----

    def compact(self, dataset: str, *, task_id: int | None = None) -> str | None:
        """把每个分区的多个文件合并成一个，去掉删除标记。生效数据不变，返回新快照 id。"""
        ds = get_dataset(dataset)
        base = self.head()
        manifest = self.manifest(base)
        entries = manifest.datasets.get(dataset, ())
        by_partition: dict[str, list[tuple[int, FileEntry]]] = {}
        for seq, e in enumerate(entries):
            by_partition.setdefault(e.partition, []).append((seq, e))
        new_entries: list[FileEntry] = []
        written: list[Path] = []
        changed = False
        try:
            for partition in sorted(by_partition):
                group = by_partition[partition]
                has_tombstones = any(
                    pl.scan_parquet(self.files_dir / e.path)
                    .filter(pl.col(DELETED))
                    .select(pl.len())
                    .collect()
                    .item()
                    for _, e in group
                )
                if len(group) == 1 and not has_tombstones:
                    new_entries.append(group[0][1])
                    continue
                changed = True
                resolved = self._resolve(ds, group)
                if resolved.height:
                    rows = resolved.with_columns(pl.lit(False).alias(DELETED))
                    entry, created = self._write_file(ds, partition, rows)
                    if created:
                        written.append(self.files_dir / entry.path)
                    new_entries.append(entry)
            if not changed:
                return None
            datasets = dict(manifest.datasets)
            datasets[dataset] = tuple(new_entries)
            return self._commit(
                Manifest(datasets),
                expected=base,
                task_id=task_id,
                note=f"整理 {dataset}",
                stats={"compacted": dataset},
            )
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise

    def gc(self, *, keep_days: float = 30, min_file_age_s: float = 24 * 3600) -> GcReport:
        """清理不再需要的快照和文件。

        保留：当前快照、钉住的快照、keep_days 天内创建的快照。其余快照的清单删除并标记为
        已清理；不被任何保留快照引用、且修改时间早于 min_file_age_s 秒前的数据文件删除
        （留出时间给正在写入、尚未提交的事务）。
        """
        now_s = self._clock() / 1000
        head = self.head()
        pinned = self.catalog.pinned()
        live: list[str] = []
        dead: list[str] = []
        for snap in self.catalog.snapshots(limit=1_000_000):
            recent = now_s - snap.created_at.timestamp() < keep_days * 86400
            if snap.id == head or snap.id in pinned or recent:
                live.append(snap.id)
            else:
                dead.append(snap.id)
        referenced = {
            (self.files_dir / e.path).resolve() for sid in live for e in self.manifest(sid).files()
        }
        for sid in dead:
            (self.manifests_dir / f"{sid}.json").unlink(missing_ok=True)
        self.catalog.mark_collected(dead)

        deleted = kept = 0
        for path in self.files_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.resolve() in referenced:
                kept += 1
                continue
            # 文件的修改时间来自真实的文件系统，所以这里用真实时间，不用注入的时钟
            if time.time() - path.stat().st_mtime < min_file_age_s:
                kept += 1
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError:  # Windows 上文件可能正被读取，下次再清理
                kept += 1
        return GcReport(collected_snapshots=len(dead), deleted_files=deleted, kept_files=kept)

    # ---- 内部 ----

    def _write_file(self, ds: Dataset, partition: str, df: pl.DataFrame) -> tuple[FileEntry, bool]:
        """写一个数据文件，按内容的 sha256 命名。返回 (条目, 是否新建)。

        内容相同的文件已经存在时直接复用（不新建），所以同样的写入得到同样的清单。
        """
        directory = self.files_dir / ds.name / partition
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f"{uuid.uuid4().hex}.tmp"
        try:
            df.select(list(ds.file_schema())).write_parquet(
                tmp, compression="zstd", statistics=True
            )
            _fsync(tmp)
            digest = _sha256(tmp)
            rel = f"{ds.name}/{partition}/{digest}.parquet"
            final = self.files_dir / rel
            created = not final.exists()
            if created:
                tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
        entry = FileEntry(path=rel, sha256=digest, rows=df.height, partition=partition)
        return entry, created

    def _commit(
        self,
        manifest: Manifest,
        *,
        expected: str | None,
        task_id: int | None,
        note: str | None,
        stats: dict[str, Any],
    ) -> str:
        snapshot_id = manifest.snapshot_id
        path = self.manifests_dir / f"{snapshot_id}.json"
        if not path.exists():
            tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(manifest.canonical())
            _fsync(tmp)
            tmp.replace(path)
        self.catalog.advance_head(
            snapshot_id, expected=expected, task_id=task_id, note=note, stats=stats
        )
        return snapshot_id


class Transaction:
    """一次写入。用 with 语句：正常结束时提交，出错时删除已写的文件、不提交。

    提交后 ``snapshot_id`` 是新快照；数据没有任何变化时为 None，当前快照不变。
    """

    def __init__(self, store: DataStore, *, task_id: int | None, note: str | None) -> None:
        self._store = store
        self._task_id = task_id
        self._note = note
        self.base = store.head()
        self._base_manifest = store.manifest(self.base)
        self._added: dict[str, list[FileEntry]] = {}
        self._stats: dict[str, dict[str, int]] = {}
        self._written: list[Path] = []
        self._done = False
        self.snapshot_id: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.abort()

    def write(self, dataset: str, df: pl.DataFrame) -> int:
        """写入（新增或覆盖）行。返回实际写入的行数：与当前数据完全相同的行不写。"""
        ds = get_dataset(dataset)
        _validate(ds, df)
        if df.height == 0:
            return 0
        current = self._current(ds, df)
        changed = df.join(current, on=list(ds.schema), how="anti", nulls_equal=True)
        self._append(ds, changed.with_columns(pl.lit(False).alias(DELETED)))
        self._count(dataset, "written", changed.height)
        return changed.height

    def delete(self, dataset: str, keys: pl.DataFrame) -> int:
        """给这些主键写删除标记。返回实际作废的行数：当前不存在的主键不写。"""
        ds = get_dataset(dataset)
        missing = [k for k in ds.keys if k not in keys.columns]
        if missing:
            raise DataValidationError(f"{dataset} 的删除需要主键列 {list(ds.keys)}，缺少 {missing}")
        keys = keys.select(pl.col(k).cast(ds.schema[k]) for k in ds.keys).unique()
        if keys.select(pl.any_horizontal(pl.all().is_null()).any()).item():
            raise DataValidationError(f"{dataset} 的删除主键不能有空值")
        current = self._current(ds, keys)
        existing = keys.join(current.select(list(ds.keys)), on=list(ds.keys), how="semi")
        tombstones = existing.with_columns(
            *(pl.lit(None, dtype=t).alias(c) for c, t in ds.schema.items() if c not in ds.keys),
            pl.lit(True).alias(DELETED),
        )
        self._append(ds, tombstones)
        self._count(dataset, "deleted", existing.height)
        return existing.height

    def commit(self) -> str | None:
        if self._done:
            return self.snapshot_id
        self._done = True
        if not any(self._added.values()):
            return None
        datasets = dict(self._base_manifest.datasets)
        for name, entries in self._added.items():
            datasets[name] = (*datasets.get(name, ()), *entries)
        try:
            self.snapshot_id = self._store._commit(  # pyright: ignore[reportPrivateUsage]
                Manifest(datasets),
                expected=self.base,
                task_id=self._task_id,
                note=self._note,
                stats=dict(self._stats),
            )
        except BaseException:
            self._remove_written()
            raise
        return self.snapshot_id

    def abort(self) -> None:
        if not self._done:
            self._done = True
            self._remove_written()

    # ---- 内部 ----

    def _current(self, ds: Dataset, like: pl.DataFrame) -> pl.DataFrame:
        """当前快照里与 like 涉及的分区、证券相同的数据，用于比对。"""
        store = self._store
        entries = list(enumerate(self._base_manifest.datasets.get(ds.name, ())))
        pending = self._added.get(ds.name, [])
        entries += [(len(entries) + i, e) for i, e in enumerate(pending)]
        partitions = set(ds.partition_of(like).unique().to_list())
        entries = [(i, e) for i, e in entries if e.partition in partitions]
        symbols = like.get_column("symbol").unique().to_list() if "symbol" in ds.keys else None
        return store._resolve(ds, entries, symbols=symbols)  # pyright: ignore[reportPrivateUsage]

    def _append(self, ds: Dataset, rows: pl.DataFrame) -> None:
        if rows.height == 0:
            return
        parts = rows.with_columns(ds.partition_of(rows).alias(_PARTITION))
        for (partition,), chunk in parts.group_by(_PARTITION, maintain_order=True):
            entry, created = self._store._write_file(  # pyright: ignore[reportPrivateUsage]
                ds, str(partition), chunk.drop(_PARTITION)
            )
            if created:  # 复用的已有文件可能被别的快照引用，中止时不能删
                self._written.append(self._store.files_dir / entry.path)
            self._added.setdefault(ds.name, []).append(entry)

    def _count(self, dataset: str, kind: str, n: int) -> None:
        stats = self._stats.setdefault(dataset, {})
        stats[kind] = stats.get(kind, 0) + n

    def _remove_written(self) -> None:
        for path in self._written:
            path.unlink(missing_ok=True)


def _validate(ds: Dataset, df: pl.DataFrame) -> None:
    problems = schema_problems(df, ds.schema)
    if not problems:
        nulls = [k for k in ds.keys if df.get_column(k).null_count()]
        if nulls:
            problems.append(f"主键列 {nulls} 有空值")
        elif df.select(pl.struct(list(ds.keys)).is_duplicated().any()).item():
            problems.append(f"{ds.keys} 有重复")
    if problems:
        raise DataValidationError(f"{ds.name}：" + "；".join(problems))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync(path: Path) -> None:
    with path.open("rb+") as f:
        os.fsync(f.fileno())
