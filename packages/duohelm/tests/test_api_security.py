"""整个 API 的 Host 和 Origin 校验（duohelm.api.security）。"""

from __future__ import annotations

import importlib
import os
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

WRITES = [
    ("post", "/api/tasks", {"type": "nope", "params": {}}),
    ("put", "/api/plugins/x/network", {"confirm": True, "digest": "x"}),
    ("delete", "/api/plugins/x/network", None),
    (
        "put",
        "/api/plugins/x/endpoints/svc",
        {"address": "127.0.0.1:1", "confirm": True, "digest": "x"},
    ),
]


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path, port=8765, web_port=5173)


def _client(settings: Settings, base_url: str) -> TestClient:
    return TestClient(create_app(settings), base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:5173",  # Web 开发服务器把 /api 代理过来，Host 是它的端口
        "http://localhost:5173",
    ],
)
def test_local_hosts_accepted(app_settings: Settings, base_url: str) -> None:
    assert _client(app_settings, base_url).get("/api/health").status_code == 200


@pytest.mark.parametrize(
    "base_url",
    [
        "http://evil.example.com:8765",  # DNS 重绑定：域名解析到 127.0.0.1，Host 仍是这个域名
        "http://127.0.0.1",  # 缺端口
        "http://127.0.0.1:9999",
        "http://192.168.1.20:8765",
        "http://localhost.evil.com:8765",
        "http://testserver",
    ],
)
def test_other_hosts_rejected(app_settings: Settings, base_url: str) -> None:
    r = _client(app_settings, base_url).get("/api/health")
    assert r.status_code == 400
    assert "Host" in r.json()["detail"]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
@pytest.mark.parametrize(
    "origin",
    [
        None,  # 写请求不带 Origin
        "http://evil.example.com",
        "http://evil.example.com:8765",
        "http://127.0.0.1:9999",
        "https://127.0.0.1:8765",
        "null",
    ],
)
def test_writes_need_platform_origin(
    app_settings: Settings, method: str, path: str, body: object, origin: str | None
) -> None:
    client = _client(app_settings, "http://127.0.0.1:8765")
    headers = {} if origin is None else {"Origin": origin}
    kwargs: dict[str, object] = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    r = client.request(method.upper(), path, **kwargs)  # pyright: ignore[reportArgumentType]
    assert r.status_code == 403
    assert "Origin" in r.json()["detail"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
)
def test_writes_from_platform_origin_pass_the_guard(app_settings: Settings, origin: str) -> None:
    """平台自己的来源通过校验：返回的是业务结果（插件不存在 404），不是 403。"""
    client = _client(app_settings, "http://127.0.0.1:8765")
    body = {"confirm": True, "digest": "x"}
    r = client.put("/api/plugins/x/network", json=body, headers={"Origin": origin})
    assert r.status_code == 404


def test_reads_do_not_need_origin(app_settings: Settings) -> None:
    client = _client(app_settings, "http://127.0.0.1:8765")
    assert client.get("/api/plugins").status_code == 200
    evil = {"Origin": "http://evil.example.com"}
    assert client.get("/api/plugins", headers=evil).status_code == 200


def test_web_port_from_env(tmp_path: Path) -> None:
    settings = Settings.from_env({"DUOHELM_HOME": str(tmp_path), "DUOHELM_WEB_PORT": "3000"})
    assert settings.web_port == 3000
    client = _client(settings, "http://127.0.0.1:3000")
    assert client.get("/api/health").status_code == 200
    with pytest.raises(ValueError, match="DUOHELM_WEB_PORT"):
        Settings.from_env({"DUOHELM_HOME": str(tmp_path), "DUOHELM_WEB_PORT": "0"})
