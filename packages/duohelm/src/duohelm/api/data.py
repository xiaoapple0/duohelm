"""行情数据的只读接口：/api/data/...。设计见 docs/subsystems/data.md 的“入库质量报告”。"""

from __future__ import annotations

import json
import re
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException

from duohelm.storage import CorruptFileError, DataStore, SnapshotNotFoundError
from duohelm.tasks.quality import REPORT_KEY

_SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}")


def read_quality_report(data: DataStore, snapshot_id: str) -> dict[str, Any] | None:
    """某个快照的质量报告；快照里没有报告（早于 #31 的快照）时为 None。

    抛 :class:`SnapshotNotFoundError`：快照不存在。
    """
    rows = data.read("quality_report", snapshot=snapshot_id)
    rows = rows.filter(pl.col("key") == REPORT_KEY)
    if not rows.height:
        return None
    report = json.loads(rows.item(0, "report"))
    if not isinstance(report, dict):
        raise CorruptFileError("质量报告不是 JSON 对象")
    return report  # pyright: ignore[reportUnknownVariableType]


def data_router(data: DataStore) -> APIRouter:
    router = APIRouter()

    def get_quality_report(snapshot_id: str) -> dict[str, Any]:
        # 先校验格式：id 会用来拼清单文件的路径
        if not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise HTTPException(404, f"找不到快照 {snapshot_id}")
        try:
            report = read_quality_report(data, snapshot_id)
        except SnapshotNotFoundError:
            raise HTTPException(404, f"找不到快照 {snapshot_id}") from None
        if report is None:
            raise HTTPException(
                404, f"快照 {snapshot_id} 没有质量报告（它早于质量报告功能，重新同步后才有）"
            )
        return report

    router.add_api_route(
        "/api/data/snapshots/{snapshot_id}/quality-report", get_quality_report, methods=["GET"]
    )
    return router
