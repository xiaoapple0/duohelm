"""定时规则的 HTTP 接口。"""

from __future__ import annotations

import importlib
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# 本地缺少依赖时跳过；在 CI 上必须失败，不能静默跳过。
for _module in ("fastapi", "httpx"):
    if os.environ.get("CI"):
        importlib.import_module(_module)
    else:
        pytest.importorskip(_module)

from fastapi.testclient import TestClient

from duohelm.api import create_app
from duohelm.settings import Settings
from duohelm_sdk import MANIFEST_FILENAME

SH = timezone(timedelta(hours=8))
LOCAL = "http://127.0.0.1:8765"
URL = "/api/schedules/data-sync"


def add_plugin(tmp_path: Path, plugin_id: str, kind: str, sdk: str = ">=0.4,<0.5") -> None:
    path = tmp_path / "plugins" / plugin_id
    path.mkdir(parents=True)
    lines = [
        "[plugin]",
        f'id = "{plugin_id}"',
        f'name = "{plugin_id}"',
        'version = "1.0.0"',
        f'kind = "{kind}"',
        f'sdk = "{sdk}"',
        f'entrypoint = "{plugin_id}:plugin"',
    ]
    (path / MANIFEST_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def make(tmp_path: Path, at: datetime) -> TestClient:
    add_plugin(tmp_path, "xtdata", "datasource")
    add_plugin(tmp_path, "eltdx", "datasource")
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
        "plugin_warning": None,
        "needs_full_sync": False,
        "state_error": None,
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


def test_enable_unknown_plugin_is_422(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    resp = client.patch(URL, json={"enabled": True, "plugin_id": "nope"})
    assert resp.status_code == 422
    assert "找不到" in resp.json()["detail"]
    assert client.get(URL).json()["enabled"] is False


def test_enable_non_datasource_is_422(tmp_path: Path) -> None:
    add_plugin(tmp_path, "myop", "operator")
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    resp = client.patch(URL, json={"enabled": True, "plugin_id": "myop"})
    assert resp.status_code == 422
    assert "行情源" in resp.json()["detail"]


def test_enable_not_ready_plugin_saves_with_warning(tmp_path: Path) -> None:
    add_plugin(tmp_path, "oldsrc", "datasource", sdk=">=9.0")  # SDK 版本不兼容，状态不是 ready
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    body = client.patch(URL, json={"enabled": True, "plugin_id": "oldsrc"}).json()
    assert body["enabled"] is True
    assert "oldsrc" in body["plugin_warning"]


def test_needs_full_sync_when_no_snapshot(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    body = client.patch(URL, json={"enabled": True, "plugin_id": "xtdata"}).json()
    assert body["needs_full_sync"] is True


def test_deleted_plugin_gets_warning(tmp_path: Path) -> None:
    client = make(tmp_path, datetime(2024, 1, 2, 10, 0, tzinfo=SH))
    assert (
        client.patch(URL, json={"enabled": True, "plugin_id": "xtdata"}).json()["plugin_warning"]
        is None
    )
    shutil.rmtree(tmp_path / "plugins" / "xtdata")
    body = client.get(URL).json()
    assert body["enabled"] is True
    assert "xtdata" in body["plugin_warning"] and "找不到" in body["plugin_warning"]
