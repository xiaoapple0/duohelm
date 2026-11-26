"""插件子进程入口：``python -I -m duohelm.isolation.child``。

启动顺序（平台自己的代码，插件代码在第 4 步之后才会被导入）：

1. 复制 fd 1 留给协议，把 fd 1 重定向到 stderr，插件的 print 和原生库输出都不会
   混进协议数据
2. 安装网络拦截（此时白名单为空，什么都不放行）
3. 发送 ``ready``，等父进程完成资源限制设置（Windows 上把进程加入 Job）后发 ``start``
4. 按 ``start`` 里的白名单重新安装网络拦截，设置插件能取到的 endpoint 地址
   （:func:`duohelm_sdk.context.endpoint`）
5. 把插件及其依赖的目录加入 ``sys.path``，按 entrypoint 加载插件对象
6. 循环处理 ``call``，直到收到 ``close`` 或 stdin 关闭
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback
from pathlib import Path
from typing import Any, BinaryIO

from duohelm.isolation import netguard
from duohelm.isolation.protocol import (
    FrameStore,
    ProtocolError,
    decode,
    encode,
    read_message,
    write_message,
)
from duohelm_sdk import Address, DataSourcePlugin, FactorPlugin
from duohelm_sdk import context as sdk_context

ALLOWED_METHODS: dict[str, frozenset[str]] = {
    "factor": frozenset({"compute"}),
    "datasource": frozenset(
        {
            "capabilities",
            "securities",
            "lookup_securities",
            "trading_calendar",
            "bars",
            "adj_factors",
            "index_constituents",
            "check_connection",
        }
    ),
}
_PROTOCOLS: dict[str, type] = {"factor": FactorPlugin, "datasource": DataSourcePlugin}


def _take_protocol_stdout() -> BinaryIO:
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "wb", buffering=0)


def _error(kind: str, exc: BaseException) -> dict[str, Any]:
    return {
        "type": "error",
        "kind": kind,
        "message": f"{type(exc).__name__}: {exc}",
        "traceback": "".join(traceback.format_exception(exc)),
    }


def _apply_permissions(start: dict[str, Any]) -> None:
    """按父进程给的白名单放行网络，设置 endpoint 地址。必须在导入插件代码之前调用。"""
    netguard.set_allowlist([str(item) for item in start.get("network", [])])
    endpoints: dict[str, Any] = start.get("endpoints", {})
    sdk_context._install(  # pyright: ignore[reportPrivateUsage]  平台专用的入口
        {str(k): Address(str(v["host"]), int(v["port"])) for k, v in endpoints.items()}
    )


def _load(start: dict[str, Any]) -> Any:
    for path in start["sys_path"]:
        sys.path.insert(0, str(path))
    module_name, _, attr = str(start["entrypoint"]).partition(":")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    kind = str(start["kind"])
    protocol = _PROTOCOLS.get(kind)
    if protocol is None:
        raise TypeError(f"M0 不支持在隔离进程里运行 {kind} 插件")
    if not isinstance(obj, protocol):
        raise TypeError(f"{start['entrypoint']} 没有实现 {protocol.__name__}")
    return obj


def serve(proto_in: BinaryIO, proto_out: BinaryIO) -> int:
    write_message(proto_out, {"type": "ready", "pid": os.getpid()})
    start = read_message(proto_in)
    if start is None or start.get("type") != "start":
        return 2
    frames = FrameStore(Path(str(start["workdir"])), "out")
    inputs = FrameStore(Path(str(start["workdir"])), "in")
    allowed = ALLOWED_METHODS.get(str(start["kind"]), frozenset())
    try:
        _apply_permissions(start)
        plugin = _load(start)
    except MemoryError as exc:
        write_message(proto_out, _error("memory", exc))
        return 1
    except BaseException as exc:
        write_message(proto_out, _error("load", exc))
        return 1
    write_message(proto_out, {"type": "started"})

    while True:
        message = read_message(proto_in)
        if message is None or message.get("type") == "close":
            return 0
        if message.get("type") != "call":
            write_message(proto_out, _error("protocol", ProtocolError("未知消息")))
            continue
        method = str(message.get("method"))
        if method not in allowed:
            write_message(proto_out, _error("protocol", ProtocolError(f"不允许调用 {method}")))
            continue
        try:
            args = decode(message.get("args", []), inputs)
            kwargs = decode(message.get("kwargs", {}), inputs)
            result = getattr(plugin, method)(*args, **kwargs)
        except MemoryError as exc:
            write_message(proto_out, _error("memory", exc))
            continue
        except Exception as exc:
            write_message(proto_out, _error("plugin_error", exc))
            continue
        try:
            write_message(proto_out, {"type": "result", "value": encode(result, frames)})
        except (ProtocolError, MemoryError) as exc:
            write_message(proto_out, _error("protocol", exc))


def main() -> int:
    proto_out = _take_protocol_stdout()
    netguard.install()
    return serve(sys.stdin.buffer, proto_out)


if __name__ == "__main__":
    raise SystemExit(main())
