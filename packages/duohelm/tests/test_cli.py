from __future__ import annotations

import io

from duohelm.cli import main


def test_plugins_command_lists_status(tmp_path, monkeypatch, make_plugin) -> None:
    # make_plugin 写入 tmp_path / "plugins"，正好是 DUOHELM_HOME=tmp_path 时的插件目录。
    monkeypatch.setenv("DUOHELM_HOME", str(tmp_path))
    make_plugin("ops-basic")
    make_plugin("ops-industry", requires={"ops-missing": ""})

    out = io.StringIO()
    assert main(["plugins"], out=out) == 0
    text = out.getvalue()
    assert "ops-basic" in text
    assert "ready" in text
    assert "blocked" in text
    assert "缺少依赖 ops-missing" in text


def test_plugins_command_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DUOHELM_HOME", str(tmp_path))
    out = io.StringIO()
    assert main(["plugins"], out=out) == 0
    assert "没有找到插件" in out.getvalue()


def test_redirected_output_does_not_crash_on_chinese(tmp_path, monkeypatch) -> None:
    """输出重定向到按 cp1252 编码的文件时（英文 Windows 上任务计划程序写日志），
    中文不能让命令崩溃。"""
    monkeypatch.setenv("DUOHELM_HOME", str(tmp_path))
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr("sys.stdout", fake)
    monkeypatch.setattr("sys.stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert main(["plugins"]) == 0
    fake.flush()
    assert "没有找到插件" in raw.getvalue().decode("utf-8")


def _serve_output(tmp_path, monkeypatch, args, web_dir=None) -> str:
    import uvicorn  # noqa: PLC0415

    monkeypatch.setenv("DUOHELM_HOME", str(tmp_path))
    if web_dir is not None:
        monkeypatch.setenv("DUOHELM_WEB_DIR", str(web_dir))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    out = io.StringIO()
    assert main(["serve", *args], out=out) == 0
    return out.getvalue()


def test_serve_prints_page_url(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    text = _serve_output(tmp_path, monkeypatch, ["--port", "8799"], dist)
    assert "Web 页面: http://127.0.0.1:8799/" in text
    text = _serve_output(tmp_path, monkeypatch, ["--host", "::1"], dist)
    assert "http://[::1]:8765/" in text
    text = _serve_output(tmp_path, monkeypatch, ["--host", "0.0.0.0"], dist)
    assert "http://127.0.0.1:8765/" in text
    assert "局域网" in text
    text = _serve_output(tmp_path, monkeypatch, ["--host", "::"], dist)
    assert "http://[::1]:8765/" in text
    text = _serve_output(tmp_path, monkeypatch, ["--host", "localhost"], dist)
    assert "http://localhost:8765/" in text
    # 只监听局域网地址：按这个地址访问会因为 Host 不对被拒绝，不打印它
    text = _serve_output(tmp_path, monkeypatch, ["--host", "192.168.1.20"], dist)
    assert "http://192.168.1.20" not in text
    assert "只能在本机打开" in text


def test_serve_without_build_explains(tmp_path, monkeypatch) -> None:
    text = _serve_output(tmp_path, monkeypatch, [], tmp_path / "no-dist")
    assert "没有找到构建好的 Web 页面" in text
    assert "npm run build" in text
    assert "Web 页面:" not in text
