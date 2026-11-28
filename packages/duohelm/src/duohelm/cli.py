"""命令行入口：``duohelm serve`` 启动 API，``duohelm plugins`` 查看插件状态。"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from dataclasses import replace
from typing import TextIO

from duohelm import __version__
from duohelm.registry import PluginRegistry
from duohelm.settings import DEFAULT_HOST, DEFAULT_PORT, Settings, is_loopback


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="duohelm", description="舵手 DuoHelm")
    parser.add_argument("--version", action="version", version=f"duohelm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 API 服务")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser("plugins", help="列出插件及其状态")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    if out is None:
        _utf8_when_redirected()
    stream: TextIO = out if out is not None else sys.stdout
    args = _parser().parse_args(argv)
    settings = Settings.from_env()

    if args.command == "plugins":
        return _plugins(settings, stream)
    if args.command == "serve":
        return _serve(replace(settings, host=args.host, port=args.port), stream)
    return 2


def _plugins(settings: Settings, out: TextIO) -> int:
    entries = PluginRegistry([settings.plugin_dir]).scan()
    if not entries:
        print(f"没有找到插件（插件目录: {settings.plugin_dir}）", file=out)
        return 0
    for e in entries:
        version = e.manifest.plugin.version if e.manifest else "-"
        kind = e.manifest.plugin.kind if e.manifest else "-"
        print(f"{e.id:<32} {version:<10} {kind:<11} {e.status}", file=out)
        for problem in e.problems:
            print(f"    - {problem}", file=out)
    return 0


def _serve(settings: Settings, out: TextIO) -> int:
    # 延迟导入：plugins 等命令不需要加载 Web 框架。
    import uvicorn  # noqa: PLC0415

    from duohelm.api import create_app  # noqa: PLC0415

    if not is_loopback(settings.host):
        print(
            f"警告：正在监听 {settings.host}。现阶段 API 没有登录保护，局域网内的任何人都能访问。",
            file=out,
        )
    from duohelm.api.web import has_web_build  # noqa: PLC0415

    if has_web_build(settings.web_dir):
        url_host = _url_host(settings.host)
        if url_host is None:
            print(
                f"Web 页面只能在本机打开：没有监听 127.0.0.1 或 [::1]，而来源校验只接受"
                f"本机地址（Host 为 127.0.0.1、localhost 或 [::1] 加端口 {settings.port}）",
                file=out,
            )
        else:
            print(f"Web 页面: http://{url_host}:{settings.port}/", file=out)
    else:
        print(
            f"没有找到构建好的 Web 页面（{settings.web_dir}），只提供 API。"
            "在仓库的 web/ 目录下运行 npm ci && npm run build 后重启；"
            "或用 DUOHELM_WEB_DIR 指定页面目录",
            file=out,
        )
    uvicorn.run(create_app(settings, run_workers=True), host=settings.host, port=settings.port)
    return 0


def _utf8_when_redirected() -> None:
    """输出重定向到文件或管道时（例如任务计划程序把输出写进日志），改用 UTF-8。

    Windows 上重定向的输出按系统的 ANSI 代码页编码（cp1252、cp936 等），打印中文会抛
    UnicodeEncodeError，serve 来不及启动就退出；日志里的中文也会变成“Logging error”。
    直接输出到控制台时 Python 本来就用 UTF-8，不改。
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _url_host(host: str) -> str | None:
    """页面在本机的地址。来源校验只接受本机地址，所以只监听局域网地址时没有能打开的地址。"""
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "[::1]"  # 只监听 IPv6 时 127.0.0.1 可能连不上
    if not is_loopback(host):
        return None
    return f"[{host}]" if ":" in host else host


if __name__ == "__main__":
    raise SystemExit(main())
