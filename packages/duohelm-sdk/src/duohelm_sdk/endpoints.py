"""用户可配置的连接地址：地址格式与 scope 规则（SDK 0.3 起）。

插件在清单的 ``[[permissions.endpoints]]`` 里声明需要连接的服务（例如 MiniQMT）和默认地址；
实际地址由用户在平台里配置并确认。平台在**保存时**和**启动插件子进程、传地址之前**各用
:func:`parse_address` 校验一次；清单里的 ``default`` 在解析清单时也按同样的规则校验。

scope 规则：

- ``loopback``：只接受 ``127.0.0.1`` 和 ``::1``
- ``lan``：只接受 IP 地址，不接受主机名（防止 DNS 重绑定）：本机（``127.0.0.1``、``::1``）、
  IPv4 私有网段（10/8、172.16/12、192.168/16）、IPv6 唯一本地地址（fc00::/7）
- ``internet``：IP 地址或主机名。平台保存时需要用户额外确认

地址格式：``主机:端口``，IPv6 写成 ``[::1]:58610``。端口必填，1–65535。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum

_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_LOOPBACK_HOSTS = frozenset({ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")})
_LAN_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class EndpointScope(StrEnum):
    LOOPBACK = "loopback"
    LAN = "lan"
    INTERNET = "internet"


class AddressError(ValueError):
    """地址格式不对，或不符合 scope。消息可直接展示给用户。"""


@dataclass(frozen=True)
class Address:
    host: str
    """IP 地址（规范写法，IPv6 不带方括号）或小写主机名。"""
    port: int

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True

    def __str__(self) -> str:
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


def parse_address(text: str, scope: EndpointScope | str) -> Address:
    """解析 ``主机:端口`` 并按 scope 校验。不访问网络，不解析域名。"""
    scope = EndpointScope(scope)
    host, port = _split(text.strip())
    ip = _ip_or_none(host)
    if ip is None:
        if scope is not EndpointScope.INTERNET:
            raise AddressError(
                f"{scope} 范围只接受 IP 地址，不接受主机名（防止 DNS 重绑定）：{host!r}"
            )
        if not _HOSTNAME.fullmatch(host):
            raise AddressError(f"主机名格式不对（只写小写主机名，不带协议和路径）：{host!r}")
        return Address(host, port)
    if ip.is_unspecified or ip.is_multicast:
        raise AddressError(f"不是可连接的地址：{ip}")
    if scope is EndpointScope.LOOPBACK and ip not in _LOOPBACK_HOSTS:
        raise AddressError(f"loopback 范围只接受 127.0.0.1 和 ::1：{ip}")
    if (
        scope is EndpointScope.LAN
        and ip not in _LOOPBACK_HOSTS
        and not any(ip.version == n.version and ip in n for n in _LAN_NETWORKS)
    ):
        raise AddressError(
            "lan 范围只接受本机（127.0.0.1、::1）或私有网段"
            f"（10/8、172.16/12、192.168/16、fc00::/7）：{ip}"
        )
    return Address(str(ip), port)


def _split(text: str) -> tuple[str, int]:
    if "/" in text or "@" in text:
        raise AddressError(f"只写 主机:端口，不带协议、用户名和路径：{text!r}")
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise AddressError(f"IPv6 地址要写成 [地址]:端口：{text!r}")
        port_text = rest[1:]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep or not host:
            raise AddressError(f"地址要写成 主机:端口：{text!r}")
        if ":" in host:
            raise AddressError(f"IPv6 地址要写成 [地址]:端口：{text!r}")
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise AddressError(f"端口必须是 1–65535 的整数：{text!r}")
    return host, int(port_text)


def _ip_or_none(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if "%" in host:
        raise AddressError(f"不接受带网卡后缀的 IPv6 地址：{host!r}")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None
