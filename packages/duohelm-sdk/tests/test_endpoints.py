from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from duohelm_sdk import (
    Address,
    AddressError,
    ConnectionStatus,
    DataSourcePlugin,
    EndpointNotConfiguredError,
    EndpointScope,
    ManifestError,
    SupportsConnectionCheck,
    endpoint,
    parse_address,
    parse_manifest,
)
from duohelm_sdk import context as ctx

# ---- 地址与 scope ----


@pytest.mark.parametrize(
    ("text", "scope", "expected"),
    [
        ("127.0.0.1:58610", "loopback", Address("127.0.0.1", 58610)),
        ("[::1]:58610", "loopback", Address("::1", 58610)),
        ("192.168.31.5:58610", "lan", Address("192.168.31.5", 58610)),
        ("10.0.0.2:1", "lan", Address("10.0.0.2", 1)),
        ("172.31.255.255:65535", "lan", Address("172.31.255.255", 65535)),
        ("127.0.0.1:58610", "lan", Address("127.0.0.1", 58610)),
        ("[fd00::5]:80", "lan", Address("fd00::5", 80)),
        ("  192.168.1.2:80  ", "lan", Address("192.168.1.2", 80)),
        ("example.com:7709", "internet", Address("example.com", 7709)),
        ("8.8.8.8:53", "internet", Address("8.8.8.8", 53)),
        ("192.168.1.2:80", "internet", Address("192.168.1.2", 80)),
    ],
)
def test_accepts(text: str, scope: str, expected: Address) -> None:
    assert parse_address(text, scope) == expected


@pytest.mark.parametrize(
    ("text", "scope", "reason"),
    [
        # loopback 只接受 127.0.0.1 和 ::1
        ("localhost:58610", "loopback", "不接受主机名"),
        ("127.0.0.2:58610", "loopback", "127.0.0.1 和 ::1"),
        ("192.168.1.2:58610", "loopback", "127.0.0.1 和 ::1"),
        # lan 只接受 IP，且必须是本机或私有网段
        ("miniqmt.lan:58610", "lan", "不接受主机名"),
        ("localhost:58610", "lan", "不接受主机名"),
        ("8.8.8.8:53", "lan", "私有网段"),
        ("172.32.0.1:80", "lan", "私有网段"),
        ("169.254.1.1:80", "lan", "私有网段"),
        ("100.64.0.1:80", "lan", "私有网段"),
        ("[fe80::1]:80", "lan", "私有网段"),
        ("[2001:db8::1]:80", "lan", "私有网段"),
        # IPv4 映射的 IPv6 地址：lan 和 loopback 都不接受（只认 IPv4 写法和 ::1）
        ("[::ffff:192.168.1.1]:58610", "lan", "私有网段"),
        ("[::ffff:127.0.0.1]:58610", "lan", "私有网段"),
        ("[::ffff:192.168.1.1]:58610", "loopback", "127.0.0.1 和 ::1"),
        ("[::ffff:127.0.0.1]:58610", "loopback", "127.0.0.1 和 ::1"),
        # 所有 scope 都不接受的
        ("0.0.0.0:80", "internet", "不是可连接的地址"),
        ("224.0.0.1:80", "internet", "不是可连接的地址"),
        ("[fe80::1%eth0]:80", "internet", "网卡后缀"),
        ("192.168.1.2", "lan", "主机:端口"),
        ("192.168.1.2:0", "lan", "端口"),
        ("192.168.1.2:65536", "lan", "端口"),
        ("192.168.1.2:http", "lan", "端口"),
        ("::1:80", "loopback", "[地址]:端口"),
        ("[::1]80", "loopback", "[地址]:端口"),
        ("http://example.com:80", "internet", "不带协议"),
        ("user@example.com:80", "internet", "不带协议"),
        ("Example.COM:80", "internet", "主机名格式"),
    ],
)
def test_rejects(text: str, scope: str, reason: str) -> None:
    with pytest.raises(AddressError, match=re.escape(reason)):
        parse_address(text, scope)


def test_unknown_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="'public'"):
        parse_address("8.8.8.8:53", "public")


def test_address_str_round_trips() -> None:
    for text in ("192.168.1.2:80", "[::1]:58610", "example.com:7709"):
        assert str(parse_address(text, EndpointScope.INTERNET)) == text
    assert parse_address("[::0001]:80", "loopback").host == "::1"  # 规范写法


# ---- 清单 ----

MANIFEST = """
[plugin]
id = "demo-source"
name = "示例行情源"
version = "0.1.0"
kind = "datasource"
sdk = ">=0.4,<0.5"
entrypoint = "demo_source:plugin"

[permissions]
network = []

[[permissions.endpoints]]
id = "miniqmt"
description = "MiniQMT 行情服务"
default = "127.0.0.1:58610"
scope = "lan"
"""


def _error(text: str) -> str:
    with pytest.raises(ManifestError) as info:
        parse_manifest(text)
    return info.value.message


def test_manifest_endpoints() -> None:
    m = parse_manifest(MANIFEST)
    (e,) = m.permissions.endpoints
    assert (e.id, e.scope, e.default) == ("miniqmt", EndpointScope.LAN, "127.0.0.1:58610")


def test_manifest_without_endpoints_still_parses() -> None:
    head = MANIFEST.split("[[permissions.endpoints]]", maxsplit=1)[0]
    assert parse_manifest(head).permissions.endpoints == ()


def _with_endpoint(default: str, scope: str) -> str:
    return MANIFEST.replace('default = "127.0.0.1:58610"', f'default = "{default}"').replace(
        'scope = "lan"', f'scope = "{scope}"'
    )


@pytest.mark.parametrize(
    ("default", "scope"),
    [
        ("8.8.8.8:58610", "loopback"),  # 声明 loopback，default 却是公网地址
        ("example.com:58610", "loopback"),
        ("192.168.1.2:58610", "loopback"),
        ("8.8.8.8:58610", "lan"),
        ("miniqmt.lan:58610", "lan"),
        ("[::ffff:192.168.1.1]:58610", "lan"),
        ("0.0.0.0:58610", "internet"),
        ("127.0.0.1", "loopback"),  # 没有端口
    ],
)
def test_manifest_default_must_fit_scope(default: str, scope: str) -> None:
    """default 在解析清单时就按它声明的 scope 校验，不合格是清单错误。

    不在平台里运行时 endpoint() 不知道 scope，所以不能指望那里发现问题。
    """
    message = _error(_with_endpoint(default, scope))
    assert "不符合 scope" in message and "miniqmt" in message


@pytest.mark.parametrize(
    ("default", "scope"),
    [
        ("127.0.0.1:58610", "loopback"),
        ("[::1]:58610", "loopback"),
        ("192.168.1.2:58610", "lan"),
        ("example.com:7709", "internet"),
    ],
)
def test_manifest_default_within_scope_parses(default: str, scope: str) -> None:
    (e,) = parse_manifest(_with_endpoint(default, scope)).permissions.endpoints
    assert (e.default, e.scope) == (default, EndpointScope(scope))


def test_manifest_rejects_bad_endpoint_fields() -> None:
    assert "scope" in _error(MANIFEST.replace('scope = "lan"', 'scope = "public"'))
    assert "id" in _error(MANIFEST.replace('id = "miniqmt"', 'id = "MiniQMT"'))
    assert "description" in _error(MANIFEST.replace('description = "MiniQMT 行情服务"\n', ""))
    dup = MANIFEST + MANIFEST[MANIFEST.index("[[permissions.endpoints]]") :]
    assert "endpoint id 重复" in _error(dup)


# ---- 运行时 ----


@pytest.fixture(autouse=True)
def _outside_platform() -> Iterator[None]:
    ctx._reset()  # pyright: ignore[reportPrivateUsage]
    yield
    ctx._reset()  # pyright: ignore[reportPrivateUsage]


def test_endpoint_outside_platform_uses_default() -> None:
    assert not ctx.in_platform()
    assert endpoint("miniqmt", default="127.0.0.1:58610") == Address("127.0.0.1", 58610)
    with pytest.raises(EndpointNotConfiguredError, match="default"):
        endpoint("miniqmt")


def test_endpoint_in_platform_ignores_default() -> None:
    """平台传入的地址（用户确认过的）优先于插件代码里写的 default。"""
    ctx._install({"miniqmt": Address("192.168.31.5", 58610)})  # pyright: ignore[reportPrivateUsage]
    assert ctx.in_platform()
    assert endpoint("miniqmt", default="127.0.0.1:58610") == Address("192.168.31.5", 58610)
    with pytest.raises(EndpointNotConfiguredError, match="没有声明"):
        endpoint("other", default="127.0.0.1:1")


def test_unconfirmed_endpoint_raises_in_platform() -> None:
    """用户还没确认的 endpoint：平台不传地址，代码里的 default 也不用，报错提示去插件页确认。"""
    ctx._install({}, unconfirmed=["miniqmt"])  # pyright: ignore[reportPrivateUsage]
    assert ctx.in_platform()
    with pytest.raises(EndpointNotConfiguredError, match="插件页确认"):
        endpoint("miniqmt", default="127.0.0.1:58610")
    with pytest.raises(EndpointNotConfiguredError, match="没有声明"):
        endpoint("other", default="127.0.0.1:1")


# ---- 可选的连接检查 ----


def test_connection_check_is_optional() -> None:
    class Checked:
        def check_connection(self) -> ConnectionStatus:
            return ConnectionStatus(ok=False, message="请打开 MiniQMT")

    class Plain:
        pass

    assert isinstance(Checked(), SupportsConnectionCheck)
    assert not isinstance(Plain(), SupportsConnectionCheck)
    assert Checked().check_connection() == ConnectionStatus(False, "请打开 MiniQMT")
    # 不是 DataSourcePlugin 的必需方法
    assert "check_connection" not in DataSourcePlugin.__dict__
