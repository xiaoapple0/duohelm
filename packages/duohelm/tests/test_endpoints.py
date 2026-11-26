"""用户配置的连接地址：存储、确认、哈希、两次校验，以及子进程里的网络白名单。"""

from __future__ import annotations

import socket
import textwrap
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from duohelm.endpoints import (
    ConfirmationRequiredError,
    EndpointConfigError,
    EndpointStore,
    UnknownEndpointError,
    checked_addresses,
    endpoints_digest,
)
from duohelm.isolation import Limits, PluginRunError, PluginSession
from duohelm.isolation.netguard import Allowlist
from duohelm.registry import PluginEntry, PluginRegistry, PluginStatus
from duohelm_sdk import Address, ConnectionStatus, Manifest, parse_manifest

MANIFEST = """
[plugin]
id = "{plugin_id}"
name = "{name}"
version = "{version}"
kind = "{kind}"
sdk = ">=0.3,<0.4"
entrypoint = "{module}:{obj}"

[permissions]
network = [{network}]

[[permissions.endpoints]]
id = "svc"
description = "{description}"
default = "{default}"
scope = "{scope}"

{factors}"""

FACTORS = """
[[factors]]
id = "double"
name = "double"
fields = ["close"]
"""


def manifest_text(
    *,
    plugin_id: str = "ep-test",
    name: str = "ep-test",
    version: str = "1.0.0",
    network: str = "",
    description: str = "测试服务",
    default: str = "127.0.0.1:58610",
    scope: str = "lan",
    kind: str = "factor",
) -> str:
    return MANIFEST.format(
        kind=kind,
        obj="plugin" if kind == "factor" else "source",
        factors=FACTORS if kind == "factor" else "",
        plugin_id=plugin_id,
        module=plugin_id.replace("-", "_"),
        name=name,
        version=version,
        network=network,
        description=description,
        default=default,
        scope=scope,
    )


def manifest(**kw: str) -> Manifest:
    return parse_manifest(manifest_text(**kw))


@pytest.fixture
def store(tmp_path: Path) -> EndpointStore:
    return EndpointStore(tmp_path / "plugin-config.db")


# ---- 只对 endpoints 那一节算哈希 ----


def test_digest_ignores_rest_of_manifest() -> None:
    base = endpoints_digest(manifest())
    assert endpoints_digest(manifest(version="2.0.0")) == base
    assert endpoints_digest(manifest(name="改名了")) == base
    assert endpoints_digest(manifest(network='"example.com:80"')) == base


@pytest.mark.parametrize(
    "change",
    [
        {"description": "换了说明"},
        {"default": "127.0.0.1:1"},
        {"scope": "internet"},
    ],
)
def test_digest_changes_with_endpoints_section(change: dict[str, str]) -> None:
    assert endpoints_digest(manifest(**change)) != endpoints_digest(manifest())


# ---- 保存与确认（第一次校验） ----


def test_default_until_confirmed(store: EndpointStore) -> None:
    m = manifest()
    (view,) = store.views(m)
    assert (view.state, view.configured, view.effective) == ("default", None, "127.0.0.1:58610")
    assert store.addresses_for(m) == {"svc": "127.0.0.1:58610"}


def test_confirm_saves_normalized_address(store: EndpointStore) -> None:
    m = manifest()
    view = store.confirm(m, "svc", " 192.168.31.5:58610 ", confirmed=True)
    assert (view.state, view.effective) == ("confirmed", "192.168.31.5:58610")
    assert store.addresses_for(m) == {"svc": "192.168.31.5:58610"}


def test_confirm_requires_confirmation(store: EndpointStore) -> None:
    with pytest.raises(ConfirmationRequiredError):
        store.confirm(manifest(), "svc", "192.168.31.5:58610", confirmed=False)
    assert store.views(manifest())[0].state == "default"
    assert store.audit("ep-test") == []


@pytest.mark.parametrize(
    ("scope", "address"),
    [
        ("lan", "miniqmt.lan:58610"),  # lan 不接受主机名
        ("lan", "8.8.8.8:58610"),
        ("lan", "[::ffff:192.168.1.1]:58610"),
        ("loopback", "192.168.31.5:58610"),
        ("loopback", "localhost:58610"),
    ],
)
def test_save_rejects_address_outside_scope(store: EndpointStore, scope: str, address: str) -> None:
    default = "127.0.0.1:58610"
    m = manifest(scope=scope, default=default)
    with pytest.raises(EndpointConfigError):
        store.confirm(m, "svc", address, confirmed=True)
    assert store.addresses_for(m) == {"svc": default}


def test_internet_needs_extra_confirmation(store: EndpointStore) -> None:
    m = manifest(scope="internet", default="example.com:7709")
    with pytest.raises(ConfirmationRequiredError, match="额外确认"):
        store.confirm(m, "svc", "tdx.example.com:7709", confirmed=True)
    view = store.confirm(m, "svc", "tdx.example.com:7709", confirmed=True, confirm_internet=True)
    assert view.effective == "tdx.example.com:7709"


def test_unknown_endpoint(store: EndpointStore) -> None:
    with pytest.raises(UnknownEndpointError):
        store.confirm(manifest(), "other", "192.168.31.5:58610", confirmed=True)


# ---- 需要重新确认 ----


def test_endpoints_change_requires_reconfirmation(store: EndpointStore) -> None:
    store.confirm(manifest(), "svc", "192.168.31.5:58610", confirmed=True)
    # 插件升版本：不需要重新确认
    assert store.addresses_for(manifest(version="1.1.0")) == {"svc": "192.168.31.5:58610"}
    # endpoints 这一节变了：已保存的地址停用，用 default，直到重新确认
    changed = manifest(description="新说明")
    (view,) = store.views(changed)
    assert view.state == "needs_confirmation"
    assert view.configured == "192.168.31.5:58610"
    assert view.effective == "127.0.0.1:58610"
    assert store.addresses_for(changed) == {"svc": "127.0.0.1:58610"}
    store.confirm(changed, "svc", "192.168.31.5:58610", confirmed=True)
    assert store.addresses_for(changed) == {"svc": "192.168.31.5:58610"}


def test_clear_and_audit(store: EndpointStore) -> None:
    m = manifest()
    store.confirm(m, "svc", "192.168.31.5:58610", confirmed=True)
    store.confirm(m, "svc", "192.168.31.6:58610", confirmed=True)
    view = store.clear(m, "svc")
    assert view.state == "default"
    store.clear(m, "svc")  # 没有配置时清除：不记审计
    audit = [(a.action, a.old_address, a.new_address) for a in store.audit("ep-test")]
    assert audit == [
        ("confirm", None, "192.168.31.5:58610"),
        ("confirm", "192.168.31.5:58610", "192.168.31.6:58610"),
        ("clear", "192.168.31.6:58610", None),
    ]
    assert all(a.endpoints_digest == endpoints_digest(m) for a in store.audit("ep-test"))


def test_store_survives_reopen(tmp_path: Path) -> None:
    EndpointStore(tmp_path / "c.db").confirm(manifest(), "svc", "10.0.0.2:1", confirmed=True)
    assert EndpointStore(tmp_path / "c.db").addresses_for(manifest()) == {"svc": "10.0.0.2:1"}


# ---- 传地址之前的第二次校验 ----


def test_checked_addresses_fills_defaults() -> None:
    assert checked_addresses(manifest(), {}) == {"svc": Address("127.0.0.1", 58610)}


@pytest.mark.parametrize("address", ["miniqmt.lan:58610", "8.8.8.8:58610", "192.168.1.2"])
def test_checked_addresses_rejects_outside_scope(address: str) -> None:
    with pytest.raises(EndpointConfigError, match="不符合 lan"):
        checked_addresses(manifest(), {"svc": address})


def test_checked_addresses_rejects_undeclared() -> None:
    with pytest.raises(UnknownEndpointError):
        checked_addresses(manifest(), {"svc": "127.0.0.1:1", "other": "127.0.0.1:2"})


# ---- netguard 白名单 ----


def test_allowlist_ip_and_port() -> None:
    allow = Allowlist(["127.0.0.1:58610", "192.168.31.5", "[::1]:80"])
    assert allow.allows("127.0.0.1", 58610)
    assert not allow.allows("127.0.0.1", 58611)
    assert allow.allows("192.168.31.5", 1) and allow.allows("192.168.31.5", 65535)
    assert allow.allows("::1", 80) and allow.allows("0:0:0:0:0:0:0:1", 80)
    assert not allow.allows("::1", 81)
    assert not allow.allows("10.0.0.1", 58610)
    assert not Allowlist().allows("127.0.0.1", 58610)


def test_allowlist_hostname_and_resolved_ips() -> None:
    allow = Allowlist(["tdx.example.com:7709"])
    assert allow.allows("TDX.example.com.", 7709, lookup=True)
    assert allow.allows("tdx.example.com", None, lookup=True)
    assert not allow.allows("tdx.example.com", 80, lookup=True)
    assert not allow.allows("203.0.113.7", 7709)  # 还没解析过
    allow.remember("tdx.example.com", ["203.0.113.7"])
    assert allow.allows("203.0.113.7", 7709)
    assert not allow.allows("203.0.113.7", 80)
    assert not allow.allows("203.0.113.7", 7709, lookup=True)  # 解析主机名只认白名单里写明的
    allow.remember("evil.example.com", ["198.51.100.1"])  # 不在白名单里的主机名：不记
    assert not allow.allows("198.51.100.1", 7709)


# ---- 子进程：白名单和 endpoint 地址 ----

PLUGIN_CODE = r"""
import socket

import polars as pl
from duohelm_sdk import endpoint
from duohelm_sdk.context import in_platform


class Plugin:
    def compute(self, factor_id, data):
        name, _, arg = factor_id.partition(":")
        if name == "endpoint":
            a = endpoint("svc", default="127.0.0.1:1")
            inside = in_platform()
            return pl.DataFrame({"host": [a.host], "port": [a.port], "in_platform": [inside]})
        if name == "connect":
            s = socket.socket()
            s.settimeout(5)
            s.connect(("127.0.0.1", int(arg)))
            s.close()
            return pl.DataFrame({"ok": [True]})
        if name == "by_name":  # 主机名：getaddrinfo + 解析出的 IP
            s = socket.create_connection(("localhost", int(arg)), timeout=5)
            s.close()
            ip = socket.gethostbyname("localhost")
            return pl.DataFrame({"ok": [True], "ip": [ip]})
        if name == "lookup":  # 只解析，不连接
            how, _, port = arg.partition("@")
            if how == "getaddrinfo":
                socket.getaddrinfo("localhost", int(port))
            else:
                socket.gethostbyname("localhost")
            return pl.DataFrame({"ok": [True]})
        if name == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(arg)
        if name == "sendmsg":
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendmsg([b"x"], [], 0, ("127.0.0.1", int(arg)))
        if name == "reverse":
            socket.gethostbyaddr("127.0.0.1")
        if name == "udp":
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"x", ("127.0.0.1", int(arg)))
            s.close()
            return pl.DataFrame({"ok": [True]})
        if name == "asyncio":
            import asyncio

            async def go():
                r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(arg)), 5)
                w.close()

            asyncio.run(go())
            return pl.DataFrame({"ok": [True]})
        raise KeyError(name)


class Source:
    # 只用来测 check_connection 能跨进程调用

    def capabilities(self): ...
    def securities(self, *, include_delisted=False): ...
    def lookup_securities(self, symbols): ...
    def trading_calendar(self, market, start, end): ...
    def bars(self, symbols, freq, start, end): ...
    def adj_factors(self, symbols, start, end): ...
    def index_constituents(self, index, start, end): ...

    def check_connection(self):
        from duohelm_sdk import ConnectionStatus

        a = endpoint("svc", default="127.0.0.1:1")
        return ConnectionStatus(ok=False, message=f"连不上 {a.port}")


source = Source()


plugin = Plugin()
"""

FAST = Limits(call_timeout_s=30, startup_timeout_s=60, poll_interval_s=0.1)
DATA = pl.DataFrame({"date": [date(2024, 1, 2)], "symbol": ["600000.SH"], "close": [10.0]})


def make_entry(root: Path, **kw: str) -> PluginEntry:
    path = root / "ep-test"
    path.mkdir(parents=True)
    (path / "ep_test.py").write_text(textwrap.dedent(PLUGIN_CODE), encoding="utf-8")
    (path / "duohelm-plugin.toml").write_text(manifest_text(**kw), encoding="utf-8")
    [entry] = PluginRegistry([root]).scan()
    assert entry.status is PluginStatus.READY, entry.problems
    return entry


@pytest.fixture
def listener() -> Iterator[tuple[int, list[str]]]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.2)
    seen: list[str] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, addr = server.accept()
            except OSError:
                continue
            seen.append(str(addr))
            conn.close()

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield server.getsockname()[1], seen
    stop.set()
    t.join()
    server.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_plugin_receives_confirmed_address(tmp_path: Path) -> None:
    entry = make_entry(tmp_path)
    with PluginSession(entry, limits=FAST, endpoints={"svc": "192.168.31.5:58610"}) as s:
        df = s.call("compute", "endpoint", DATA)
    assert df.row(0) == ("192.168.31.5", 58610, True)


def test_plugin_receives_default_without_config(tmp_path: Path) -> None:
    entry = make_entry(tmp_path)
    with PluginSession(entry, limits=FAST) as s:
        df = s.call("compute", "endpoint", DATA)
    assert df.row(0) == ("127.0.0.1", 58610, True)


def test_network_allowlist_from_manifest(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    port, seen = listener
    entry = make_entry(tmp_path, network=f'"127.0.0.1:{port}"')
    with PluginSession(entry, limits=FAST) as s:
        assert s.call("compute", f"connect:{port}", DATA)["ok"].item() is True
        with pytest.raises(PluginRunError, match="没有网络权限"):
            s.call("compute", f"connect:{_free_port()}", DATA)
    assert len(seen) == 1


def test_endpoint_address_is_allowed(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    port, seen = listener
    entry = make_entry(tmp_path, scope="loopback")
    with PluginSession(entry, limits=FAST, endpoints={"svc": f"127.0.0.1:{port}"}) as s:
        assert s.call("compute", f"connect:{port}", DATA)["ok"].item() is True
    assert len(seen) == 1


def test_endpoint_default_port_only(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    """没有配置地址时，只放行 default 的端口，不放行别的端口。"""
    port, seen = listener
    entry = make_entry(tmp_path, scope="loopback")
    with (
        PluginSession(entry, limits=FAST) as s,
        pytest.raises(PluginRunError, match="没有网络权限"),
    ):
        s.call("compute", f"connect:{port}", DATA)
    assert seen == []


def test_hostname_in_allowlist(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    """白名单写主机名：解析放行，解析出的 IP 按同样的端口放行。"""
    port, seen = listener
    entry = make_entry(tmp_path, network=f'"localhost:{port}"')
    with PluginSession(entry, limits=FAST) as s:
        df = s.call("compute", f"by_name:{port}", DATA)
        assert df["ok"].item() is True
        # 解析出的 127.0.0.1 只放行白名单里的端口
        with pytest.raises(PluginRunError, match="没有网络权限"):
            s.call("compute", f"connect:{_free_port()}", DATA)
    assert len(seen) == 1


def test_hostname_not_in_allowlist(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    port, seen = listener
    entry = make_entry(tmp_path, network=f'"127.0.0.1:{port}"')
    with PluginSession(entry, limits=FAST) as s, pytest.raises(PluginRunError, match="localhost"):
        s.call("compute", f"by_name:{port}", DATA)
    assert seen == []


@pytest.mark.parametrize("how", ["getaddrinfo", "gethostbyname"])
def test_lookup_needs_hostname_in_allowlist(tmp_path: Path, how: str) -> None:
    """只解析不连接，也要求主机名在白名单里。"""
    denied = make_entry(tmp_path / "d", network='"127.0.0.1:80"')
    with (
        PluginSession(denied, limits=FAST) as s,
        pytest.raises(PluginRunError, match="没有网络权限"),
    ):
        s.call("compute", f"lookup:{how}@80", DATA)
    allowed = make_entry(tmp_path / "a", network='"localhost:80"')
    with PluginSession(allowed, limits=FAST) as s:
        assert s.call("compute", f"lookup:{how}@80", DATA)["ok"].item() is True


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Windows 的 Python 没有 AF_UNIX")
def test_unix_socket_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # pyright: ignore[reportAttributeAccessIssue]
    server.bind(str(path))
    server.listen()
    try:
        entry = make_entry(tmp_path / "p", network='"127.0.0.1"')
        with (
            PluginSession(entry, limits=FAST) as s,
            pytest.raises(PluginRunError, match="不能连接"),
        ):
            s.call("compute", f"unix:{path}", DATA)
    finally:
        server.close()


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="Windows 的 Python 没有 sendmsg")
def test_sendmsg_refused_even_if_allowed(tmp_path: Path) -> None:
    entry = make_entry(tmp_path, network='"127.0.0.1"')
    with PluginSession(entry, limits=FAST) as s, pytest.raises(PluginRunError, match="sendmsg"):
        s.call("compute", f"sendmsg:{_free_port()}", DATA)


def test_reverse_lookup_always_refused(tmp_path: Path) -> None:
    entry = make_entry(tmp_path, network='"127.0.0.1"')
    with (
        PluginSession(entry, limits=FAST) as s,
        pytest.raises(PluginRunError, match="gethostbyaddr"),
    ):
        s.call("compute", "reverse", DATA)


def test_udp_sendto(tmp_path: Path) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    port = int(receiver.getsockname()[1])
    try:
        allowed = make_entry(tmp_path / "a", network=f'"127.0.0.1:{port}"')
        with PluginSession(allowed, limits=FAST) as s:
            assert s.call("compute", f"udp:{port}", DATA)["ok"].item() is True
        assert receiver.recv(16) == b"x"
        blocked = make_entry(tmp_path / "b", network=f'"127.0.0.1:{port + 1}"')
        with (
            PluginSession(blocked, limits=FAST) as s,
            pytest.raises(PluginRunError, match="没有网络权限"),
        ):
            s.call("compute", f"udp:{port}", DATA)
    finally:
        receiver.close()


def test_asyncio_connection_is_guarded(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    """Windows 上 asyncio 的 Proactor 不经过 socket.connect，也要拦截。"""
    port, seen = listener
    blocked = make_entry(tmp_path / "b")
    with (
        PluginSession(blocked, limits=FAST) as s,
        pytest.raises(PluginRunError, match="没有网络权限"),
    ):
        s.call("compute", f"asyncio:{port}", DATA)
    assert seen == []
    allowed = make_entry(tmp_path / "a", network=f'"127.0.0.1:{port}"')
    with PluginSession(allowed, limits=FAST) as s:
        assert s.call("compute", f"asyncio:{port}", DATA)["ok"].item() is True
    assert len(seen) == 1


def test_check_connection_crosses_process(tmp_path: Path) -> None:
    entry = make_entry(tmp_path, kind="datasource")
    with PluginSession(entry, limits=FAST, endpoints={"svc": "127.0.0.1:58611"}) as s:
        status = s.call("check_connection")
    assert status == ConnectionStatus(ok=False, message="连不上 58611")


@pytest.mark.parametrize("address", ["miniqmt.lan:58610", "8.8.8.8:58610"])
def test_session_refuses_address_outside_scope(tmp_path: Path, address: str) -> None:
    """第二次校验：地址不符合 scope 时不启动子进程。"""
    entry = make_entry(tmp_path)
    session = PluginSession(entry, limits=FAST, endpoints={"svc": address})
    with pytest.raises(PluginRunError) as info:
        session.start()
    assert info.value.reason == "load"
    assert "不符合 lan" in info.value.message
    assert session.workdir is None  # 还没有创建会话目录，也没有启动进程
    session.close()


def test_session_refuses_undeclared_endpoint(tmp_path: Path) -> None:
    entry = make_entry(tmp_path)
    session = PluginSession(entry, limits=FAST, endpoints={"other": "127.0.0.1:1"})
    with pytest.raises(PluginRunError, match="没有声明"):
        session.start()
    session.close()
