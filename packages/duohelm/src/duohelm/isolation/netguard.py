"""Python 层的网络拦截。在子进程里、导入插件代码之前安装。

**只防误用，不防恶意。** 它拦截通过 Python ``socket`` 模块发起的连接、地址解析和
UDP 发送，因此 requests、httpx、urllib 等纯 Python 的网络访问，除了白名单里的地址都会
失败；但原生扩展、ctypes 直接调用系统接口、厂商提供的 DLL / so 都可以绕过它（例如
xtquant 的 C 扩展连接 MiniQMT）。威胁模型见 docs/subsystems/plugins.md。

白名单由平台在 ``start`` 消息里给出：清单的 ``permissions.network``，加上 endpoint 的地址
（启动前已按 scope 校验）。每一项是 ``主机`` 或 ``主机:端口``（IPv6 写成 ``[地址]:端口``）；
不带端口的项放行这个主机的所有端口。

- 连接 IP 地址：IP 和端口必须匹配白名单里的一项
- 解析主机名：主机名必须在白名单里；解析出来的 IP 记下来，之后连接这些 IP（按主机名那一项
  的端口规则）也放行
- 其他一律拒绝：反向解析、Unix 套接字、``sendmsg``，以及白名单为空时的一切网络访问

``socket.socketpair()`` 例外：asyncio 等在 Windows 上用它在本机建一对互联的套接字做
内部唤醒，它不访问网络。
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable, Iterable
from typing import Any, cast

_state = threading.local()


class NetworkBlockedError(PermissionError):
    pass


def _norm_host(host: object) -> str:
    text = str(host).strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text.rstrip(".")


def _split_entry(entry: str) -> tuple[str, int | None]:
    text = entry.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:  # 不带端口的主机名或 IPv4，或不带方括号的 IPv6
        host, port = text, ""
    return _norm_host(host), (int(port) if port else None)


def _as_port(port: object) -> int | None:
    if isinstance(port, int):
        return port
    if isinstance(port, str) and port.isdigit():
        return int(port)
    return None


class Allowlist:
    """放行的主机和端口。端口为 ``None`` 表示这个主机的所有端口。"""

    def __init__(self, items: Iterable[str] = ()) -> None:
        self._entries: dict[str, set[int | None]] = {}
        for item in items:
            host, port = _split_entry(item)
            self._entries.setdefault(host, set()).add(port)
        self._resolved: dict[str, set[int | None]] = {}
        self._lock = threading.Lock()

    def _ports(self, host: str, *, include_resolved: bool) -> set[int | None]:
        ports = set(self._entries.get(host, ()))
        if include_resolved:
            with self._lock:
                ports |= self._resolved.get(host, set())
        return ports

    def allows(self, host: object, port: object, *, lookup: bool = False) -> bool:
        """``lookup`` 为 True 时只认白名单里写明的主机（解析主机名用），不认解析出来的 IP。"""
        ports = self._ports(_norm_host(host), include_resolved=not lookup)
        if not ports or None in ports:
            return bool(ports)
        if lookup and port is None:
            return True  # 只解析、不指定端口：主机在白名单里即可
        return _as_port(port) in ports

    def remember(self, host: object, ips: Iterable[object]) -> None:
        """记下白名单主机名解析出来的 IP，之后连接它们按主机名那一项的端口规则放行。"""
        ports = self._ports(_norm_host(host), include_resolved=False)
        if not ports:
            return
        with self._lock:
            for ip in ips:
                self._resolved.setdefault(_norm_host(ip), set()).update(ports)


_allow = Allowlist()


def _refuse(host: object, port: object) -> NetworkBlockedError:
    name = _norm_host(host)
    shown = f"[{name}]" if ":" in name else name
    what = f"{shown}:{port}" if port is not None else shown
    return NetworkBlockedError(
        f"DuoHelm：插件没有网络权限访问 {what}（清单的 permissions.network 和 endpoints 里都没有）"
    )


def _passthrough() -> bool:
    return bool(getattr(_state, "in_socketpair", False))


def _check_address(address: object) -> None:
    if isinstance(address, tuple):  # (host, port, ...)：AF_INET / AF_INET6
        parts = cast(tuple[object, ...], address)
        if len(parts) < 2:
            raise NetworkBlockedError(f"DuoHelm：插件不能连接 {address!r}")
        host, port = parts[0], parts[1]
        if not _allow.allows(host, port):
            raise _refuse(host, port)
        return
    raise NetworkBlockedError(f"DuoHelm：插件不能连接 {address!r}")  # Unix 套接字路径等


def _wrap_connect(original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _passthrough():
            _check_address(address)
        return original(self, address, *args, **kwargs)

    guarded.__name__ = getattr(original, "__name__", "guarded")
    return guarded


def _wrap_sendto(original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(self: socket.socket, *args: Any) -> Any:
        if not _passthrough():
            _check_address(args[-1] if args else None)
        return original(self, *args)

    guarded.__name__ = "sendto"
    return guarded


def _refuse_always(name: str) -> Callable[..., Any]:
    def guarded(*_args: Any, **_kwargs: Any) -> Any:
        raise NetworkBlockedError(f"DuoHelm：插件不能调用 socket.{name}")

    guarded.__name__ = name
    return guarded


def _check_lookup(host: object, port: object = None) -> None:
    if not (_allow.allows(host, port) or _allow.allows(host, port, lookup=True)):
        raise _refuse(host, port)


_installed = False


def set_allowlist(allowed: Iterable[str]) -> None:
    """设置白名单。子进程收到 ``start`` 后、导入插件代码之前调用一次。"""
    global _allow  # noqa: PLW0603  进程级设置
    _allow = Allowlist(allowed)


def install(allowed: Iterable[str] = ()) -> None:
    """替换 socket 模块里会访问网络的入口。安装后不可撤销；重复调用只更新白名单。"""
    global _installed  # noqa: PLW0603
    set_allowlist(allowed)
    if _installed:
        return
    _installed = True

    sock_cls: Any = socket.socket
    for name in ("connect", "connect_ex"):
        setattr(sock_cls, name, _wrap_connect(getattr(sock_cls, name)))
    sock_cls.sendto = _wrap_sendto(sock_cls.sendto)
    if hasattr(sock_cls, "sendmsg"):
        sock_cls.sendmsg = _refuse_always("sendmsg")

    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_create_connection = socket.create_connection

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if not _passthrough():
            _check_lookup(host, port)
        result = original_getaddrinfo(host, port, *args, **kwargs)
        _allow.remember(host, (info[4][0] for info in result))
        return result

    def gethostbyname(host: Any) -> str:
        _check_lookup(host)
        ip = original_gethostbyname(host)
        _allow.remember(host, [ip])
        return ip

    def gethostbyname_ex(host: Any) -> Any:
        _check_lookup(host)
        result = original_gethostbyname_ex(host)
        _allow.remember(host, result[2])
        return result

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        _check_lookup(address[0], address[1])
        return original_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.create_connection = create_connection
    socket.gethostbyaddr = _refuse_always("gethostbyaddr")

    original_socketpair = socket.socketpair

    def socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
        _state.in_socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            _state.in_socketpair = False

    socket.socketpair = socketpair
