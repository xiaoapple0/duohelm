from __future__ import annotations

import importlib
import os
import time
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
from duohelm.tasks import TaskStore, TaskTypeRegistry, Worker
from duohelm.tasks.builtin import builtin_task_types

# API 只接受本机的 Host，写请求要带平台自己的 Origin（duohelm.api.security）
LOCAL = "http://127.0.0.1:8765"


def make(tmp_path: Path, *, dev: bool = False) -> tuple[TestClient, Worker]:
    settings = Settings(home=tmp_path, dev=dev)
    client = TestClient(create_app(settings), base_url=LOCAL, headers={"Origin": LOCAL})
    worker = Worker(TaskStore(settings.db_path), TaskTypeRegistry(builtin_task_types(settings)))
    return client, worker


def test_task_types(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    names = [t["name"] for t in client.get("/api/task-types").json()]
    assert names == ["data.cross_check", "data.sync", "plugins.scan"]


def test_data_sync_params_are_validated(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    bad = {"plugin_id": "xtdata", "start": "2024-01-05", "end": "2024-01-02"}
    assert client.post("/api/tasks", json={"type": "data.sync", "params": bad}).status_code == 422
    missing = client.post("/api/tasks", json={"type": "data.sync", "params": {}})
    assert missing.status_code == 422


def test_demo_sleep_only_in_dev(tmp_path: Path) -> None:
    client, _ = make(tmp_path, dev=True)
    types = {t["name"]: t for t in client.get("/api/task-types").json()}
    assert "demo.sleep" in types
    assert "seconds" in types["demo.sleep"]["params_schema"]["properties"]

    prod, _ = make(tmp_path / "prod")
    resp = prod.post("/api/tasks", json={"type": "demo.sleep"})
    assert resp.status_code == 422


def test_create_and_run(tmp_path: Path, make_plugin) -> None:
    make_plugin("ops-basic")
    client, worker = make(tmp_path)
    resp = client.post("/api/tasks", json={"type": "plugins.scan"})
    assert resp.status_code == 201
    task = resp.json()
    assert task["status"] == "queued"
    assert task["max_attempts"] == 3

    assert worker.run_once()
    done = client.get(f"/api/tasks/{task['id']}").json()
    assert done["status"] == "succeeded"
    assert done["result"] == {"total": 1, "by_status": {"ready": 1}}
    assert done["progress"] == 1.0


def test_unknown_type_rejected(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    resp = client.post("/api/tasks", json={"type": "nope"})
    assert resp.status_code == 422
    assert "nope" in resp.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [{"seconds": -1}, {"seconds": "abc"}, {"unexpected": 1}],
)
def test_params_validated(tmp_path: Path, params: dict[str, object]) -> None:
    client, _ = make(tmp_path, dev=True)
    resp = client.post("/api/tasks", json={"type": "demo.sleep", "params": params})
    assert resp.status_code == 422


def test_params_normalized(tmp_path: Path) -> None:
    client, _ = make(tmp_path, dev=True)
    task = client.post("/api/tasks", json={"type": "demo.sleep", "params": {"seconds": 1}}).json()
    assert task["params"] == {"seconds": 1.0, "steps": 10, "fail_attempts": 0}


def test_dedupe_conflict(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    body = {"type": "plugins.scan", "dedupe_key": "scan"}
    assert client.post("/api/tasks", json=body).status_code == 201
    assert client.post("/api/tasks", json=body).status_code == 409


def test_cancel_and_retry(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    task_id = client.post("/api/tasks", json={"type": "plugins.scan"}).json()["id"]

    resp = client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 409

    resp = client.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 409


def test_retry_after_failures_extends_attempts(tmp_path: Path) -> None:
    client, worker = make(tmp_path, dev=True)
    body = {"type": "demo.sleep", "params": {"seconds": 0.01, "steps": 1, "fail_attempts": 5}}
    task_id = client.post("/api/tasks", json=body).json()["id"]
    store = worker.store
    for _ in range(3):
        # 跳过退避等待：直接把 run_after 视为已到
        worker.run_once()
        with store._write() as conn:  # pyright: ignore[reportPrivateUsage]
            conn.execute("UPDATE tasks SET run_after = 0 WHERE id = ?", (task_id,))
    failed = client.get(f"/api/tasks/{task_id}").json()
    assert failed["status"] == "failed"
    assert failed["attempt"] == 3

    retried = client.post(f"/api/tasks/{task_id}/retry").json()
    assert retried["attempt"] == 3
    assert retried["max_attempts"] == 6


def test_retry_dedupe_conflict(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    body = {"type": "plugins.scan", "dedupe_key": "scan"}
    first = client.post("/api/tasks", json=body).json()["id"]
    client.post(f"/api/tasks/{first}/cancel")
    assert client.post("/api/tasks", json=body).status_code == 201
    assert client.post(f"/api/tasks/{first}/retry").status_code == 409


def test_not_found(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    assert client.get("/api/tasks/999").status_code == 404
    assert client.get("/api/tasks/999/logs").status_code == 404
    assert client.post("/api/tasks/999/cancel").status_code == 404
    assert client.post("/api/tasks/999/retry").status_code == 404


def test_logs_incremental(tmp_path: Path) -> None:
    client, worker = make(tmp_path)
    task_id = client.post("/api/tasks", json={"type": "plugins.scan"}).json()["id"]
    worker.run_once()
    logs = client.get(f"/api/tasks/{task_id}/logs").json()
    assert len(logs) >= 3
    assert all(log["attempt"] == 1 for log in logs)
    tail = client.get(f"/api/tasks/{task_id}/logs", params={"after_id": logs[1]["id"]}).json()
    assert [log["id"] for log in tail] == [log["id"] for log in logs[2:]]


def test_list_filters(tmp_path: Path) -> None:
    client, _ = make(tmp_path)
    ids = [client.post("/api/tasks", json={"type": "plugins.scan"}).json()["id"] for _ in range(3)]
    client.post(f"/api/tasks/{ids[0]}/cancel")
    listed = client.get("/api/tasks").json()
    assert [t["id"] for t in listed] == ids[::-1]
    cancelled = client.get("/api/tasks", params={"status": "cancelled"}).json()
    assert [t["id"] for t in cancelled] == [ids[0]]
    page = client.get("/api/tasks", params={"before_id": ids[2], "limit": 1}).json()
    assert [t["id"] for t in page] == [ids[1]]
    assert client.get("/api/tasks", params={"status": "bogus"}).status_code == 422
    assert client.get("/api/tasks", params={"limit": 0}).status_code == 422


def test_serve_lifespan_runs_workers(tmp_path: Path) -> None:
    app = create_app(Settings(home=tmp_path), run_workers=True)
    with TestClient(app, base_url=LOCAL, headers={"Origin": LOCAL}) as client:
        task_id = client.post("/api/tasks", json={"type": "plugins.scan"}).json()["id"]
        deadline = time.monotonic() + 10
        status = ""
        while time.monotonic() < deadline:
            status = client.get(f"/api/tasks/{task_id}").json()["status"]
            if status == "succeeded":
                break
            time.sleep(0.1)
        assert status == "succeeded"
