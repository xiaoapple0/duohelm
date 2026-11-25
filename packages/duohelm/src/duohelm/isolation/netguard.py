"""Python 层的网络拦截。在子进程里、导入插件代码之前安装。

**只防误用，不防恶意。** 它拦截通过 Python ``socket`` 模块发起的连接、地址解析和
UDP 发送，因此 requests、httpx、urllib 等纯 Python 的网络访问都会失败；但原生扩展、
ctypes 直接调用系统接口、厂商提供的 DLL / so 都可以绕过它。威胁模型见
docs/subsystems/plugins.md。

M0 不放行任何主机。``socket.socketpair()`` 例外：asyncio 等在 Windows 上用它在本机
建一对互联的套接字做内部唤醒，它不访问网络。
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from typing import Any

_state = threading.local()


class NetworkBlockedError(PermissionError):
    pass


def _blocked(*_args: object, **_kwargs: object) -> Any:
    if getattr(_state, "in_socketpair", False):
        return None
    raise NetworkBlockedError("DuoHelm：插件没有网络权限（M0 不放行任何主机）")


def _guard(original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        if getattr(_state, "in_socketpair", False):
            return original(*args, **kwargs)
        return _blocked()

    guarded.__name__ = getattr(original, "__name__", "guarded")
    return guarded


def install() -> None:
    """替换 socket 模块里会访问网络的入口。安装后不可撤销。"""
    sock_cls: Any = socket.socket
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(sock_cls, name):
            setattr(sock_cls, name, _guard(getattr(sock_cls, name)))

    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "create_connection",
    ):
        setattr(socket, name, _guard(getattr(socket, name)))

    original_socketpair = socket.socketpair

    def socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
        _state.in_socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            _state.in_socketpair = False

    socket.socketpair = socketpair
