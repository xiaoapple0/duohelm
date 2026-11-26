"""定时规则的 HTTP 接口。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from duohelm.api import create_app
from duohelm.settings import Settings

SH = timezone(timedelta(hours=8))
LOCAL = "http://127.0.0.1:8765"
URL = "/api/schedules/data-sync"


def make(tmp_path: Path, at: datetime) -> TestClient:
    settings = Settings(home=tmp_path)
    app = create_app(settings, now=lambda: at)
    return TestClient(app, base_url=LOCAL, headers={"Origin": LOCAL})


def test_default_is_disabled(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    body = client.get(URL).json()
    assert body == {
        "enabled": False,
        "plugin_id": None,
        "time": "16:00",
        "timezone": "Asia/Shanghai",
        "last_trigger_date": None,
        "next_run_at": None,
    }


def test_enable_and_next_run(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 5, 10, 0, tzinfo=SH))  # 周五上午
    resp = client.patch(URL, json={"enabled": True, "plugin_id": "xtdata", "time": "16:30"})
    assert resp.status_code == 200
    body = resp.json()
    assert (body["enabled"], body["plugin_id"], body["time"]) == (True, "xtdata", "16:30")
    assert datetime.fromisoformat(body["next_run_at"]) == datetime(2024, 1, 5, 16, 30, tzinfo=SH)
    assert client.get(URL).json() == body


def test_patch_keeps_unspecified_values(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    client.patch(URL, json={"enabled": True, "plugin_id": "xtdata", "time": "17:15"})
    body = client.patch(URL, json={"enabled": False}).json()
    assert (body["enabled"], body["plugin_id"], body["time"]) == (False, "xtdata", "17:15")
    assert body["next_run_at"] is None


@pytest.mark.parametrize("bad", ["15:30", "15:00", "9:30", "abc", "16:00:30"])
def test_bad_time_is_422_and_not_saved(tmp_path: Path, bad: str) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    assert client.patch(URL, json={"time": bad}).status_code == 422
    assert client.get(URL).json()["time"] == "16:00"


def test_enable_without_plugin_is_422(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    assert client.patch(URL, json={"enabled": True}).status_code == 422
    assert client.get(URL).json()["enabled"] is False
