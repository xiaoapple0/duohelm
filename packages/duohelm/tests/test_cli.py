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
