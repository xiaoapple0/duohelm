"""命令行入口：``duohelm serve`` 启动 API，``duohelm plugins`` 查看插件状态。"""

from __future__ import annotations

import argparse
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
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
