"""serve 托管构建好的 Web 页面（duohelm.api.web）。"""

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
from duohelm.settings import REPO_WEB_DIR, Settings

INDEX_HTML = "<!doctype html><title>DuoHelm</title>"
ASSET_JS = "console.log('app')"
SECRET = "outside-the-web-dir"


@pytest.fixture
def web_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text(ASSET_JS, encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    return dist


def _client(tmp_path: Path, web_dir: Path | None, base_url: str = "http://127.0.0.1:8765"):
    settings = Settings(home=tmp_path / "home", port=8765, web_port=5173, web_dir=web_dir)
    return TestClient(create_app(settings), base_url=base_url)


@pytest.mark.parametrize("path", ["/", "/sync", "/plugins", "/tasks/123", "/no/such/page"])
def test_pages_fall_back_to_index(tmp_path: Path, web_dir: Path, path: str) -> None:
    """BrowserRouter 的页面地址都返回 index.html，由前端路由处理。"""
    r = _client(tmp_path, web_dir).get(path)
    assert r.status_code == 200
    assert r.text == INDEX_HTML
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"


def test_files_in_dist_are_served(tmp_path: Path, web_dir: Path) -> None:
    client = _client(tmp_path, web_dir)
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.text == ASSET_JS
    assert "javascript" in r.headers["content-type"]
    assert "immutable" in r.headers["cache-control"]
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_media_types_do_not_depend_on_mimetypes(
    tmp_path: Path, web_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 注册表把 .js 登记成 text/plain 时，页面仍要拿到 JavaScript 类型。"""
    monkeypatch.setattr("starlette.responses.guess_type", lambda *a, **k: ("text/plain", None))
    client = _client(tmp_path, web_dir)
    assert (
        client.get("/assets/index-abc123.js").headers["content-type"].startswith("text/javascript")
    )
    assert client.get("/").headers["content-type"].startswith("text/html")
    assert client.get("/favicon.svg").headers["content-type"] == "image/svg+xml"


def test_api_routes_take_precedence(tmp_path: Path, web_dir: Path) -> None:
    client = _client(tmp_path, web_dir)
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/plugins").json() == []
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/no-such-endpoint", "/api/plugins/x/y"])
def test_unknown_api_paths_are_404_not_the_page(tmp_path: Path, web_dir: Path, path: str) -> None:
    r = _client(tmp_path, web_dir).get(path)
    assert r.status_code in {404, 405}
    assert INDEX_HTML not in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/..%5csecret.txt",
        "/assets%5c..%5c..%5csecret.txt",
        "/%5c%5cserver%5cshare%5cx",
        "/C:/Windows/win.ini",
        "/C:%5cWindows%5cwin.ini",
    ],
)
def test_no_files_outside_dist(tmp_path: Path, web_dir: Path, path: str) -> None:
    r = _client(tmp_path, web_dir).get(path)
    assert SECRET not in r.text
    assert "[fonts]" not in r.text  # win.ini 的内容
    if r.status_code == 200:
        assert r.text == INDEX_HTML


@pytest.mark.parametrize(
    "path",
    [
        "/%5c%5cserver%5cshare%5cx",  # \\server\share\x：解析它会去连那台机器
        "/C:%5cWindows%5cwin.ini",
        "/C:/Windows/win.ini",
        "/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
    ],
)
def test_suspicious_paths_rejected_before_touching_filesystem(
    tmp_path: Path, web_dir: Path, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, web_dir)

    def no_resolve(self: Path, strict: bool = False) -> Path:
        raise AssertionError(f"不应解析 {self}")

    monkeypatch.setattr(Path, "resolve", no_resolve)
    r = client.get(path)
    assert r.status_code == 200
    assert r.text == INDEX_HTML


def test_normal_paths_still_resolved(tmp_path: Path, web_dir: Path) -> None:
    """对照上一条：正常的文件路径要解析（检查符号链接是否指出目录）。"""
    client = _client(tmp_path, web_dir)
    assert client.get("/assets/index-abc123.js").text == ASSET_JS


def test_symlink_out_of_dist_not_followed(tmp_path: Path, web_dir: Path) -> None:
    link = web_dir / "leak.txt"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("本机不允许创建符号链接")
    r = _client(tmp_path, web_dir).get("/leak.txt")
    assert SECRET not in r.text


@pytest.mark.parametrize(
    "base_url",
    [
        "http://evil.example.com:8765",  # DNS 重绑定
        "http://192.168.1.20:8765",
        "http://127.0.0.1:9999",
    ],
)
def test_page_needs_local_host(tmp_path: Path, web_dir: Path, base_url: str) -> None:
    """Host 校验对页面同样生效：DNS 重绑定的网页读不到页面，也读不到 API。"""
    client = _client(tmp_path, web_dir, base_url)
    for path in ("/", "/sync", "/assets/index-abc123.js"):
        r = client.get(path)
        assert r.status_code == 400
        assert INDEX_HTML not in r.text


@pytest.mark.parametrize(
    "base_url", ["http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"]
)
def test_same_origin_writes_pass(tmp_path: Path, web_dir: Path, base_url: str) -> None:
    """页面由 serve 自己提供时，浏览器写请求的 Origin 是 API 端口本身，必须放行。"""
    client = _client(tmp_path, web_dir, base_url)
    assert client.get("/").text == INDEX_HTML
    r = client.put(
        "/api/plugins/x/network",
        json={"confirm": True, "digest": "x"},
        headers={"Origin": base_url},
    )
    assert r.status_code == 404  # 业务结果（插件不存在），不是 403


def test_cross_origin_writes_still_rejected(tmp_path: Path, web_dir: Path) -> None:
    client = _client(tmp_path, web_dir)
    r = client.put(
        "/api/plugins/x/network",
        json={"confirm": True, "digest": "x"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


def test_page_is_get_only(tmp_path: Path, web_dir: Path) -> None:
    r = _client(tmp_path, web_dir).post("/sync", headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 405


@pytest.mark.parametrize("make_dir", [False, True])
def test_without_build_only_api(tmp_path: Path, make_dir: bool) -> None:
    """没有构建产物（目录不存在，或目录里没有 index.html）时只提供 API，/ 是 404。"""
    dist = tmp_path / "dist"
    if make_dir:
        dist.mkdir()
    client = _client(tmp_path, dist)
    assert client.get("/").status_code == 404
    assert client.get("/api/health").status_code == 200
    assert _client(tmp_path, None).get("/").status_code == 404


def test_web_dir_from_env(tmp_path: Path, web_dir: Path) -> None:
    settings = Settings.from_env({"DUOHELM_HOME": str(tmp_path), "DUOHELM_WEB_DIR": str(web_dir)})
    assert settings.web_dir == web_dir
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
    assert client.get("/sync").text == INDEX_HTML


def test_default_web_dir_is_repo_build() -> None:
    assert Settings.from_env({}).web_dir == REPO_WEB_DIR
    assert (REPO_WEB_DIR.parent / "package.json").is_file()
