"""质量报告的只读接口：/api/data/snapshots/{id}/quality-report（#31）。"""

from __future__ import annotations

import importlib
import os
from datetime import date
from pathlib import Path

import pytest

# 本地缺少依赖时跳过；在 CI 上必须失败，不能静默跳过。
for _module in ("fastapi", "httpx"):
    if os.environ.get("CI"):
        importlib.import_module(_module)
    else:
        pytest.importorskip(_module)

import polars as pl
from fastapi.testclient import TestClient

from duohelm.api import create_app
from duohelm.settings import Settings
from duohelm.storage import DataStore
from duohelm.tasks.quality import REPORT_KEY, encode_report

LOCAL = "http://127.0.0.1:8765"


def make(tmp_path: Path) -> tuple[TestClient, DataStore]:
    settings = Settings(home=tmp_path)
    client = TestClient(create_app(settings), base_url=LOCAL)
    return client, DataStore(settings.data_dir)


def report_frame(report: dict[str, object]) -> pl.DataFrame:
    return pl.DataFrame({"key": [REPORT_KEY], "report": [encode_report(report)]})


def commit(data: DataStore, *, report: dict[str, object] | None) -> str:
    with data.transaction() as tx:
        tx.write("calendar", pl.DataFrame({"market": ["CN"], "date": [date(2024, 1, 2)]}))
        if report is not None:
            tx.write("quality_report", report_frame(report))
    assert tx.snapshot_id is not None
    return tx.snapshot_id


def test_reads_report_of_a_given_snapshot_not_the_head(tmp_path: Path) -> None:
    client, data = make(tmp_path)
    first = commit(data, report={"version": 1, "symbols": 1})
    with data.transaction() as tx:
        tx.write("quality_report", report_frame({"symbols": 2}))
    assert data.head() != first
    resp = client.get(f"/api/data/snapshots/{first}/quality-report")
    assert resp.status_code == 200
    assert resp.json() == {"version": 1, "symbols": 1}
    assert client.get(f"/api/data/snapshots/{data.head()}/quality-report").json() == {"symbols": 2}


def test_snapshot_without_report_is_404_not_empty(tmp_path: Path) -> None:
    client, data = make(tmp_path)
    old = commit(data, report=None)
    resp = client.get(f"/api/data/snapshots/{old}/quality-report")
    assert resp.status_code == 404
    assert "没有质量报告" in resp.json()["detail"]


@pytest.mark.parametrize("snapshot_id", ["0" * 64, "abc", "..%2F..%2Fcatalog", "A" * 64])
def test_unknown_or_malformed_snapshot_is_404(tmp_path: Path, snapshot_id: str) -> None:
    client, _ = make(tmp_path)
    assert client.get(f"/api/data/snapshots/{snapshot_id}/quality-report").status_code == 404


def test_report_endpoint_is_read_only(tmp_path: Path) -> None:
    client, data = make(tmp_path)
    snap = commit(data, report={"version": 1})
    resp = client.post(
        f"/api/data/snapshots/{snap}/quality-report", headers={"Origin": LOCAL}, json={}
    )
    assert resp.status_code == 405


def test_health_works_when_data_dir_is_broken(tmp_path: Path) -> None:
    """数据目录有问题（这里是一个同名文件）不能影响启动和 health：DataStore 按请求构造。"""
    settings = Settings(home=tmp_path)
    settings.data_dir.parent.mkdir(parents=True, exist_ok=True)
    settings.data_dir.write_text("不是目录", encoding="utf-8")
    client = TestClient(create_app(settings), base_url=LOCAL)
    assert client.get("/api/health").status_code == 200
