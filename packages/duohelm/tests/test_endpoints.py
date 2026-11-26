"""用户配置的连接地址：存储、确认、哈希、两次校验，以及子进程里的网络白名单。"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
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
    checked_network,
    endpoints_digest,
    network_digest,
)
from duohelm.isolation import Limits, PluginRunError, PluginSession, session_for
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
    assert endpoints_digest(manifest(network='"example.com:80"')) == base  # network 另算


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
    audit = [(a.target, a.action, a.old_value, a.new_value) for a in store.audit("ep-test")]
    assert audit == [
        ("endpoint:svc", "confirm", None, "192.168.31.5:58610"),
        ("endpoint:svc", "confirm", "192.168.31.5:58610", "192.168.31.6:58610"),
        ("endpoint:svc", "clear", "192.168.31.6:58610", None),
    ]
    assert all(a.digest == endpoints_digest(m) for a in store.audit("ep-test"))


def test_store_survives_reopen(tmp_path: Path) -> None:
    EndpointStore(tmp_path / "c.db").confirm(manifest(), "svc", "10.0.0.2:1", confirmed=True)
    assert EndpointStore(tmp_path / "c.db").addresses_for(manifest()) == {"svc": "10.0.0.2:1"}


# ---- 传地址之前的第二次校验 ----


def test_checked_addresses_fills_defaults() -> None:
    assert checked_addresses(manifest(), {}) == {"svc": Address("127.0.0.1", 58610)}


@pytest.mark.parametrize("address", ["miniqmt.lan:58610", "8.8.8.8:58610", "192.168.1.2"])
def test_checked_addresses_rejects_outside_scope(address: str) -> None:
    with pytest.raises(EndpointConfigError, match="不符合 lan") as info:
        checked_addresses(manifest(), {"svc": address})
    assert address.split(":", maxsplit=1)[0] not in str(info.value)  # 不写地址


def test_checked_addresses_rejects_undeclared() -> None:
    with pytest.raises(UnknownEndpointError):
        checked_addresses(manifest(), {"svc": "127.0.0.1:1", "other": "127.0.0.1:2"})


# ---- network：整个列表由用户确认 ----

NETWORK = '"8.8.8.8:53", "api.example.com"'


def test_network_needs_approval_before_use(store: EndpointStore) -> None:
    m = manifest(network=NETWORK)
    view = store.network_view(m)
    assert (view.state, view.hosts, view.approved_hosts) == (
        "needs_approval",
        ("8.8.8.8:53", "api.example.com"),
        None,
    )
    assert store.network_for(m) == []  # 确认前不能联网
    with pytest.raises(ConfirmationRequiredError):
        store.approve_network(m, confirmed=False)
    assert store.network_for(m) == []
    view = store.approve_network(m, confirmed=True)
    assert view.state == "approved"
    assert store.network_for(m) == ["8.8.8.8:53", "api.example.com"]


def test_network_list_change_requires_reapproval(store: EndpointStore) -> None:
    store.approve_network(manifest(network=NETWORK), confirmed=True)
    # 插件升版本、改 endpoints：network 不需要重新确认
    assert store.network_for(manifest(network=NETWORK, version="2.0.0", description="x")) != []
    # 顺序不同不算变化
    reordered = manifest(network='"api.example.com", "8.8.8.8:53"')
    assert store.network_view(reordered).state == "approved"
    # 多了一个地址：需要重新确认，之前不能联网
    grown = manifest(network=NETWORK + ', "data.example.com:443"')
    view = store.network_view(grown)
    assert view.state == "needs_approval"
    assert view.approved_hosts == ("8.8.8.8:53", "api.example.com")
    assert store.network_for(grown) == []
    store.approve_network(grown, confirmed=True)
    assert len(store.network_for(grown)) == 3


def test_network_revoke_and_audit(store: EndpointStore) -> None:
    m = manifest(network=NETWORK)
    store.approve_network(m, confirmed=True)
    view = store.revoke_network(m)
    assert view.state == "needs_approval"
    assert store.network_for(m) == []
    store.revoke_network(m)  # 没有确认过时撤销：不记审计
    audit = [(a.target, a.action) for a in store.audit("ep-test")]
    assert audit == [("network", "approve_network"), ("network", "revoke_network")]
    assert all(a.digest == network_digest(m) for a in store.audit("ep-test"))


def test_no_network_declared(store: EndpointStore) -> None:
    m = manifest()
    assert store.network_view(m).state == "none"
    assert store.network_for(m) == []
    with pytest.raises(EndpointConfigError, match="没有声明 network"):
        store.approve_network(m, confirmed=True)


def test_checked_network_must_be_declared() -> None:
    m = manifest(network=NETWORK)
    assert checked_network(m, ["8.8.8.8:53"]) == ["8.8.8.8:53"]
    with pytest.raises(EndpointConfigError, match="不在清单的 network 里"):
        checked_network(m, ["1.1.1.1:53"])


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
    assert not allow.allows("8.8.4.4", 7709)  # 还没解析过
    allow.remember("tdx.example.com", ["8.8.4.4"])
    assert allow.allows("8.8.4.4", 7709)
    assert not allow.allows("8.8.4.4", 80)
    assert not allow.allows("8.8.4.4", 7709, lookup=True)  # 解析主机名只认白名单里写明的
    allow.remember("evil.example.com", ["1.1.1.1"])  # 不在白名单里的主机名：不记
    assert not allow.allows("1.1.1.1", 7709)
    allow.remember("tdx.example.com", ["10.0.0.1", "127.0.0.1"])  # 私有、本机地址：不记
    assert not allow.allows("10.0.0.1", 7709)
    assert not allow.allows("127.0.0.1", 7709)


DNS_SCRIPT = r"""
import json
import socket

# 在安装 netguard 之前换掉解析函数：测试不访问真实 DNS
FAKE = {
    "data.example.com": ["8.8.8.8"],
    "evil.example.com": ["127.0.0.1"],
    "mixed.example.com": ["8.8.8.8", "192.168.1.5"],
    "127.1": ["127.0.0.1"],  # 不规范的 IP 写法，系统解析器会把它当成 127.0.0.1
}


def fake_getaddrinfo(host, port, *args, **kwargs):
    if host not in FAKE:
        raise socket.gaierror("unknown")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in FAKE[host]]


def fake_gethostbyname(host):
    return FAKE[host][0]


socket.getaddrinfo = fake_getaddrinfo
socket.gethostbyname = fake_gethostbyname

# 记录实际交给 C 层的地址，不真的连接
sent = []
socket.socket.connect = lambda self, address: sent.append(("connect", address))
socket.socket.sendto = lambda self, data, address: sent.append(("sendto", address))

from duohelm.isolation import netguard

netguard.install(
    ["data.example.com:80", "evil.example.com:80", "mixed.example.com:80", "127.1:80"]
)
results = {}


def attempt(name, fn):
    try:
        fn()
        results[name] = "ok"
    except netguard.NetworkBlockedError as exc:
        results[name] = "blocked: " + str(exc)


attempt("data", lambda: socket.getaddrinfo("data.example.com", 80))
attempt("data_ip_80", lambda: netguard._check_address(("8.8.8.8", 80)))
attempt("data_ip_81", lambda: netguard._check_address(("8.8.8.8", 81)))
attempt("evil", lambda: socket.getaddrinfo("evil.example.com", 80))
attempt("evil_byname", lambda: socket.gethostbyname("evil.example.com"))
attempt("evil_ip", lambda: netguard._check_address(("127.0.0.1", 80)))
attempt("mixed", lambda: socket.getaddrinfo("mixed.example.com", 80))
attempt("other", lambda: socket.getaddrinfo("other.example.com", 80))
attempt("other_byname", lambda: socket.gethostbyname("other.example.com"))
attempt("numeric", lambda: socket.getaddrinfo("127.1", 80))

# 直接用主机名调 connect / sendto：先走受检的解析，再用解析出的 IP
tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
attempt("connect_data", lambda: tcp.connect(("data.example.com", 80)))
attempt("sendto_data", lambda: udp.sendto(b"x", ("data.example.com", 80)))
attempt("connect_evil", lambda: tcp.connect(("evil.example.com", 80)))
attempt("sendto_evil", lambda: udp.sendto(b"x", ("evil.example.com", 80)))
attempt("connect_numeric", lambda: tcp.connect(("127.1", 80)))
attempt("connect_other", lambda: tcp.connect(("other.example.com", 80)))
results["sent"] = [[kind, list(addr)] for kind, addr in sent]
print(json.dumps(results, ensure_ascii=False))
"""


def test_hostname_must_resolve_to_public_ip(tmp_path: Path) -> None:
    """白名单里的主机名：解析到公网 IP 才放行；解析到本机或私有网段（哪怕只有一个）就拦截。

    netguard 安装后不可撤销，所以放在单独的进程里跑；解析函数换成假的，不访问真实 DNS。
    """
    script = tmp_path / "dns_check.py"
    script.write_text(DNS_SCRIPT, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    results = json.loads(done.stdout)
    assert results["data"] == "ok"
    assert results["data_ip_80"] == "ok"  # 解析出的公网 IP 按主机名那一项的端口放行
    assert results["data_ip_81"].startswith("blocked")
    for name in ("evil", "evil_byname", "mixed"):
        assert "解析到本机或私有网段" in results[name], results[name]
    assert results["evil_ip"].startswith("blocked")  # 私有地址没有被记下来
    for name in ("other", "other_byname", "connect_other"):
        assert "没有网络权限" in results[name], results[name]
    # 直接用主机名连接：实际交给 C 层的是解析出的公网 IP，内网的根本没有发出去
    assert results["connect_data"] == "ok"
    assert results["sendto_data"] == "ok"
    for name in ("numeric", "connect_evil", "sendto_evil", "connect_numeric"):
        assert "解析到本机或私有网段" in results[name], results[name]
    assert results["sent"] == [["connect", ["8.8.8.8", 80]], ["sendto", ["8.8.8.8", 80]]]


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
        if name == "allows":  # 子进程里实际生效的白名单
            from duohelm.isolation import netguard

            host, _, port = arg.rpartition(":")
            return pl.DataFrame({"ok": [netguard._allow.allows(host, int(port))]})
        if name == "connect":
            s = socket.socket()
            s.settimeout(5)
            s.connect(("127.0.0.1", int(arg)))
            s.close()
            return pl.DataFrame({"ok": [True]})
        if name == "lookup":  # 只解析，不连接
            if arg == "getaddrinfo":
                socket.getaddrinfo("localhost", 80)
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


def loopback(tmp_path: Path, port: int) -> tuple[PluginEntry, dict[str, str]]:
    """本机的连接只能走 loopback 范围的 endpoint（network 不允许本机地址）。"""
    entry = make_entry(tmp_path, scope="loopback")
    return entry, {"svc": f"127.0.0.1:{port}"}


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


def test_confirmed_address_reaches_plugin(tmp_path: Path) -> None:
    """完整路径：用户在平台里确认的地址，经 session_for 传到插件里的 endpoint()。"""
    entry = make_entry(tmp_path / "plugins")
    assert entry.manifest is not None
    store = EndpointStore(tmp_path / "plugin-config.db")
    with session_for(entry, store, limits=FAST) as s:
        assert s.call("compute", "endpoint", DATA).row(0)[:2] == ("127.0.0.1", 58610)
    store.confirm(entry.manifest, "svc", "192.168.31.5:58610", confirmed=True)
    with session_for(entry, store, limits=FAST) as s:
        assert s.call("compute", "endpoint", DATA).row(0)[:2] == ("192.168.31.5", 58610)


def test_plugin_receives_default_without_config(tmp_path: Path) -> None:
    entry = make_entry(tmp_path)
    with PluginSession(entry, limits=FAST) as s:
        df = s.call("compute", "endpoint", DATA)
    assert df.row(0) == ("127.0.0.1", 58610, True)


def test_network_reaches_child_only_after_approval(tmp_path: Path) -> None:
    """完整路径：清单的 network 在用户确认前不放行，确认后经 session_for 进入子进程的白名单。"""
    entry = make_entry(tmp_path / "plugins", network=NETWORK)
    assert entry.manifest is not None
    store = EndpointStore(tmp_path / "plugin-config.db")
    with session_for(entry, store, limits=FAST) as s:
        assert s.call("compute", "allows:8.8.8.8:53", DATA)["ok"].item() is False
    store.approve_network(entry.manifest, confirmed=True)
    with session_for(entry, store, limits=FAST) as s:
        assert s.call("compute", "allows:8.8.8.8:53", DATA)["ok"].item() is True
        assert s.call("compute", "allows:8.8.8.8:80", DATA)["ok"].item() is False


def test_session_without_network_param_allows_none(tmp_path: Path) -> None:
    """直接构造 PluginSession 且不传 network：清单的 network 一个都不放行。"""
    entry = make_entry(tmp_path, network=NETWORK)
    with PluginSession(entry, limits=FAST) as s:
        assert s.call("compute", "allows:8.8.8.8:53", DATA)["ok"].item() is False


def test_session_refuses_network_not_in_manifest(tmp_path: Path) -> None:
    entry = make_entry(tmp_path, network=NETWORK)
    session = PluginSession(entry, limits=FAST, network=["1.1.1.1:53"])
    with pytest.raises(PluginRunError, match="不在清单的 network 里") as info:
        session.start()
    assert info.value.reason == "load"
    assert session.workdir is None
    session.close()


def test_endpoint_address_is_allowed(tmp_path: Path, listener: tuple[int, list[str]]) -> None:
    port, seen = listener
    entry, endpoints = loopback(tmp_path, port)
    with PluginSession(entry, limits=FAST, endpoints=endpoints) as s:
        assert s.call("compute", f"connect:{port}", DATA)["ok"].item() is True
        with pytest.raises(PluginRunError, match="没有网络权限"):
            s.call("compute", f"connect:{_free_port()}", DATA)
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


@pytest.mark.parametrize("how", ["getaddrinfo", "gethostbyname"])
def test_lookup_needs_hostname_in_allowlist(tmp_path: Path, how: str) -> None:
    """只解析不连接，也要求主机名在白名单里。"""
    entry = make_entry(tmp_path, network=NETWORK)
    store = EndpointStore(tmp_path / "plugin-config.db")
    assert entry.manifest is not None
    store.approve_network(entry.manifest, confirmed=True)
    with (
        session_for(entry, store, limits=FAST) as s,
        pytest.raises(PluginRunError, match="没有网络权限"),
    ):
        s.call("compute", f"lookup:{how}", DATA)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Windows 的 Python 没有 AF_UNIX")
def test_unix_socket_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # pyright: ignore[reportAttributeAccessIssue]
    server.bind(str(path))
    server.listen()
    try:
        entry, endpoints = loopback(tmp_path / "p", _free_port())
        with (
            PluginSession(entry, limits=FAST, endpoints=endpoints) as s,
            pytest.raises(PluginRunError, match="不能连接"),
        ):
            s.call("compute", f"unix:{path}", DATA)
    finally:
        server.close()


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="Windows 的 Python 没有 sendmsg")
def test_sendmsg_refused_even_if_allowed(tmp_path: Path) -> None:
    port = _free_port()
    entry, endpoints = loopback(tmp_path, port)
    with (
        PluginSession(entry, limits=FAST, endpoints=endpoints) as s,
        pytest.raises(PluginRunError, match="sendmsg"),
    ):
        s.call("compute", f"sendmsg:{port}", DATA)


def test_reverse_lookup_always_refused(tmp_path: Path) -> None:
    entry, endpoints = loopback(tmp_path, _free_port())
    with (
        PluginSession(entry, limits=FAST, endpoints=endpoints) as s,
        pytest.raises(PluginRunError, match="gethostbyaddr"),
    ):
        s.call("compute", "reverse", DATA)


def test_udp_sendto(tmp_path: Path) -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    port = int(receiver.getsockname()[1])
    try:
        allowed, endpoints = loopback(tmp_path / "a", port)
        with PluginSession(allowed, limits=FAST, endpoints=endpoints) as s:
            assert s.call("compute", f"udp:{port}", DATA)["ok"].item() is True
        assert receiver.recv(16) == b"x"
        blocked, other = loopback(tmp_path / "b", port + 1)
        with (
            PluginSession(blocked, limits=FAST, endpoints=other) as s,
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
    allowed, endpoints = loopback(tmp_path / "a", port)
    with PluginSession(allowed, limits=FAST, endpoints=endpoints) as s:
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
    assert address.split(":", maxsplit=1)[0] not in info.value.message  # 错误会进任务日志，不写地址
    assert session.workdir is None  # 还没有创建会话目录，也没有启动进程
    session.close()


def test_session_refuses_undeclared_endpoint(tmp_path: Path) -> None:
    entry = make_entry(tmp_path)
    session = PluginSession(entry, limits=FAST, endpoints={"other": "127.0.0.1:1"})
    with pytest.raises(PluginRunError, match="没有声明"):
        session.start()
    session.close()
