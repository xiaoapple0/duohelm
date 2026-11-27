"""行情读取的 HTTP 接口：/api/data。只读，是 duohelm.storage.reader.MarketReader 的薄封装。

设计见 docs/subsystems/data.md 的“读取接口”一节。数据不存在返回 404，参数不合法返回 422。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from duohelm.storage import DataStore, SnapshotInfo
from duohelm.storage.reader import (
    AdjustMode,
    DataNotFoundError,
    InvalidQueryError,
    MarketReader,
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SNAPSHOT_PATTERN = r"^[0-9a-f]{64}$"

SnapshotParam = Annotated[
    str | None, Query(pattern=_SNAPSHOT_PATTERN, description="快照 id，默认当前快照")
]
DateParam = Annotated[str | None, Query(description="ISO 日期 YYYY-MM-DD（北京时间的交易日）")]


class SnapshotOut(BaseModel):
    id: str
    parent: str | None
    created_at: datetime
    task_id: int | None
    note: str | None
    stats: dict[str, Any]
    is_current: bool

    @classmethod
    def from_info(cls, info: SnapshotInfo, current: str | None) -> SnapshotOut:
        return cls(
            id=info.id,
            parent=info.parent,
            created_at=info.created_at,
            task_id=info.task_id,
            note=info.note,
            stats=info.stats,
            is_current=info.id == current,
        )


class SnapshotsOut(BaseModel):
    current: str | None
    snapshots: list[SnapshotOut]


class SecurityOut(BaseModel):
    symbol: str
    name: str | None
    exchange: str | None
    kind: str | None
    list_date: date | None
    delist_date: date | None


class SecuritiesOut(BaseModel):
    snapshot: str
    total: int
    items: list[SecurityOut]


class BarOut(BaseModel):
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    amount: float | None


class BarsOut(BaseModel):
    snapshot: str
    symbol: str
    adjust: AdjustMode
    base_date: date | None
    last_bar_date: date | None
    empty: bool
    """证券存在但所读区间内没有日线。证券不存在是 404。"""
    bars: list[BarOut]


class CoverageRowOut(BaseModel):
    symbol: str
    name: str | None
    kind: str | None
    first_date: date | None
    last_date: date | None
    bar_count: int
    expected_days: int | None
    missing_days: int | None
    """按 CN 交易日历在首末日期之间应有而缺失的天数，停牌日也算缺失。"""
    off_calendar_bars: int | None


class CoverageOut(BaseModel):
    snapshot: str
    total: int
    items: list[CoverageRowOut]


def _rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    return df.to_dicts()


def _iso_date(name: str, raw: str | None) -> date | None:
    if raw is None:
        return None
    if not _ISO_DATE.fullmatch(raw):
        raise HTTPException(422, f"{name} 必须是 YYYY-MM-DD 格式的日期：{raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, f"{name} 不是有效的日期：{raw!r}") from None


@contextmanager
def _mapped() -> Generator[None]:
    """核心库的异常对应 HTTP 状态：数据不存在 404，参数不合法 422。"""
    try:
        yield
    except DataNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    except InvalidQueryError as e:
        raise HTTPException(422, str(e)) from None


def data_router(data_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api/data", tags=["data"])
    lock = threading.Lock()
    cache: list[MarketReader] = []

    def reader() -> MarketReader:
        # 第一次读取时才创建（DataStore 会建目录和目录库），装配应用本身不碰数据目录
        with lock:
            if not cache:
                cache.append(MarketReader(DataStore(data_dir)))
            return cache[0]

    @router.get("/snapshots", response_model=SnapshotsOut)
    def list_snapshots(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> SnapshotsOut:
        current, infos = reader().snapshots(limit=limit)
        return SnapshotsOut(
            current=current, snapshots=[SnapshotOut.from_info(i, current) for i in infos]
        )

    @router.get("/snapshots/current", response_model=SnapshotOut)
    def current_snapshot() -> SnapshotOut:
        with _mapped():
            info = reader().current_snapshot()
        return SnapshotOut.from_info(info, info.id)

    @router.get("/securities", response_model=SecuritiesOut)
    def securities(
        *,
        snapshot: SnapshotParam = None,
        symbol: str | None = None,
        name_prefix: str | None = None,
        kind: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> SecuritiesOut:
        with _mapped():
            page = reader().securities(
                snapshot=snapshot,
                symbol=symbol,
                name_prefix=name_prefix,
                kind=kind,
                limit=limit,
                offset=offset,
            )
        return SecuritiesOut(
            snapshot=page.snapshot,
            total=page.total,
            items=[SecurityOut(**r) for r in _rows(page.items)],
        )

    @router.get("/securities/{symbol}/bars", response_model=BarsOut)
    def bars(
        symbol: str,
        *,
        snapshot: SnapshotParam = None,
        start: DateParam = None,
        end: DateParam = None,
        adjust: AdjustMode = "none",
        base_date: DateParam = None,
    ) -> BarsOut:
        start_d = _iso_date("start", start)
        end_d = _iso_date("end", end)
        base_d = _iso_date("base_date", base_date)
        with _mapped():
            result = reader().bars(
                symbol,
                snapshot=snapshot,
                start=start_d,
                end=end_d,
                adjust=adjust,
                base_date=base_d,
            )
        return BarsOut(
            snapshot=result.snapshot,
            symbol=result.symbol,
            adjust=result.adjust,
            base_date=result.base_date,
            last_bar_date=result.last_bar_date,
            empty=result.empty,
            bars=[BarOut(**r) for r in _rows(result.bars)],
        )

    @router.get("/coverage", response_model=CoverageOut)
    def coverage(
        *,
        snapshot: SnapshotParam = None,
        kind: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> CoverageOut:
        with _mapped():
            page = reader().coverage(snapshot=snapshot, kind=kind, limit=limit, offset=offset)
        return CoverageOut(
            snapshot=page.snapshot,
            total=page.total,
            items=[CoverageRowOut(**r) for r in _rows(page.items)],
        )

    return router
