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
