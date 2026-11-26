from __future__ import annotations

import importlib
import os
from collections.abc import Callable
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
from duohelm.endpoints import EndpointStore
from duohelm.settings import Settings
from duohelm_sdk import parse_manifest

MakePlugin = Callable[..., Path]  # 与 conftest 的 make_plugin 夹具一致

MANIFEST = """
[plugin]
id = "xt"
name = "xt"
version = "{version}"
kind = "datasource"
sdk = ">=0.3,<0.4"
entrypoint = "xt:plugin"

[[permissions.endpoints]]
id = "miniqmt"
description = "{description}"
default = "127.0.0.1:58610"
scope = "{scope}"
"""


def manifest(version: str = "1.0.0", description: str = "MiniQMT", scope: str = "lan") -> str:
    return MANIFEST.format(version=version, description=description, scope=scope)


@pytest.fixture
def client(tmp_path: Path, make_plugin: MakePlugin) -> TestClient:
    make_plugin("xt", raw=manifest())
    return TestClient(create_app(Settings(home=tmp_path)))


def _rewrite(tmp_path: Path, text: str) -> None:
    (tmp_path / "plugins" / "xt" / "duohelm-plugin.toml").write_text(text, encoding="utf-8")


def test_plugins_list_includes_endpoints(client: TestClient) -> None:
    (plugin,) = client.get("/api/plugins").json()
    (ep,) = plugin["endpoints"]
    assert ep["id"] == "miniqmt" and ep["scope"] == "lan"
    assert (ep["state"], ep["configured"], ep["effective"]) == ("default", None, "127.0.0.1:58610")


def test_confirm_and_clear(client: TestClient, tmp_path: Path) -> None:
    url = "/api/plugins/xt/endpoints/miniqmt"
    r = client.put(url, json={"address": "192.168.31.5:58610", "confirm": True})
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["effective"]) == ("confirmed", "192.168.31.5:58610")
    (ep,) = client.get("/api/plugins/xt/endpoints").json()
    assert ep["effective"] == "192.168.31.5:58610"
    # 地址保存在 DUOHELM_HOME 下的数据库里
    store = EndpointStore(tmp_path / "plugin-config.db")
    assert store.addresses_for(parse_manifest(manifest())) == {"miniqmt": "192.168.31.5:58610"}
    r = client.delete(url)
    assert (r.status_code, r.json()["state"]) == (200, "default")


def test_confirm_must_be_true(client: TestClient) -> None:
    url = "/api/plugins/xt/endpoints/miniqmt"
    assert (
        client.put(url, json={"address": "192.168.31.5:58610", "confirm": False}).status_code == 422
    )
    assert client.put(url, json={"address": "192.168.31.5:58610"}).status_code == 422
    (ep,) = client.get("/api/plugins/xt/endpoints").json()
    assert ep["state"] == "default"


@pytest.mark.parametrize("address", ["miniqmt.lan:58610", "8.8.8.8:58610", "192.168.31.5"])
def test_save_rejects_address_outside_scope(client: TestClient, address: str) -> None:
    r = client.put("/api/plugins/xt/endpoints/miniqmt", json={"address": address, "confirm": True})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_internet_needs_extra_confirmation(tmp_path: Path, make_plugin: MakePlugin) -> None:
    make_plugin("xt", raw=manifest(scope="internet"))
    client = TestClient(create_app(Settings(home=tmp_path)))
    url = "/api/plugins/xt/endpoints/miniqmt"
    body = {"address": "qmt.example.com:58610", "confirm": True}
    assert client.put(url, json=body).status_code == 409
    assert client.put(url, json={**body, "confirm_internet": True}).status_code == 200


def test_unknown_plugin_or_endpoint(client: TestClient) -> None:
    body = {"address": "192.168.31.5:58610", "confirm": True}
    assert client.put("/api/plugins/nope/endpoints/miniqmt", json=body).status_code == 404
    assert client.put("/api/plugins/xt/endpoints/nope", json=body).status_code == 404
    assert client.get("/api/plugins/nope/endpoints").status_code == 404


def test_reconfirmation_only_when_endpoints_change(client: TestClient, tmp_path: Path) -> None:
    url = "/api/plugins/xt/endpoints/miniqmt"
    client.put(url, json={"address": "192.168.31.5:58610", "confirm": True})
    _rewrite(tmp_path, manifest(version="2.0.0"))  # 插件升版本：不需要重新确认
    (ep,) = client.get("/api/plugins/xt/endpoints").json()
    assert (ep["state"], ep["effective"]) == ("confirmed", "192.168.31.5:58610")
    _rewrite(tmp_path, manifest(version="2.0.0", description="MiniQMT（新）"))
    (ep,) = client.get("/api/plugins/xt/endpoints").json()
    assert ep["state"] == "needs_confirmation"
    assert (ep["configured"], ep["effective"]) == ("192.168.31.5:58610", "127.0.0.1:58610")
    client.put(url, json={"address": "192.168.31.5:58610", "confirm": True})
    (ep,) = client.get("/api/plugins/xt/endpoints").json()
    assert ep["state"] == "confirmed"
