from __future__ import annotations

import importlib
import os

import pytest

# 本地缺少依赖时跳过；在 CI 上必须失败，不能静默跳过。
for _module in ("fastapi", "httpx"):
    if os.environ.get("CI"):
        importlib.import_module(_module)
    else:
        pytest.importorskip(_module)

from fastapi.testclient import TestClient

from duohelm import __version__
from duohelm.api import create_app
from duohelm.settings import Settings

# API 只接受本机的 Host，写请求要带平台自己的 Origin（duohelm.api.security）
LOCAL = "http://127.0.0.1:8765"


def test_health(tmp_path) -> None:
    client = TestClient(
        create_app(Settings(home=tmp_path)), base_url=LOCAL, headers={"Origin": LOCAL}
    )
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_plugins(tmp_path, make_plugin) -> None:
    make_plugin("ops-basic")
    make_plugin("broken", raw="not toml [")
    client = TestClient(
        create_app(Settings(home=tmp_path)), base_url=LOCAL, headers={"Origin": LOCAL}
    )
    plugins = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert plugins["ops-basic"]["status"] == "ready"
    assert plugins["ops-basic"]["kind"] == "operator"
    assert plugins["broken"]["status"] == "invalid"
    assert plugins["broken"]["name"] is None
