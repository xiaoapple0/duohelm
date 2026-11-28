"""插件的网络权限：清单的 ``permissions.network`` 和用户配置的连接地址
（``[[permissions.endpoints]]``），都由用户在平台里确认。

network（SDK 0.4 起只允许公网地址）：

- 整个列表由用户确认一次（:meth:`EndpointStore.approve_network`）；确认前插件不能联网
- 列表的哈希（:func:`network_digest`）变了需要重新确认，重新确认之前插件不能联网

endpoints：

规则见 docs/subsystems/plugins.md 的“用户可配置的连接地址”：

- 地址由用户在平台里填写并确认后才保存，保存时按 endpoint 的 scope 校验（第一次校验）
- 启动插件子进程、传地址之前，:class:`duohelm.isolation.runner.PluginSession` 再按 scope
  校验一次（第二次校验，见 :func:`checked_addresses`）
- 是否需要重新确认只看清单里 endpoints 那一节的哈希（:func:`endpoints_digest`）：插件升版本、
  改描述等清单其他部分的改动不需要重新确认；这一节变了，已保存的地址在重新确认之前不使用
- **没确认过的 endpoint 一律不放行**，包括清单的 default：平台不把任何地址交给插件，
  插件的 ``endpoint()`` 抛 ``EndpointNotConfiguredError``；网络白名单里也没有它
- 每次确认、清除、撤销都记审计日志（只在本机数据库里）。普通日志不写具体地址

地址保存在 ``DUOHELM_HOME/plugin-config.db``，不进任何仓库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from duohelm_sdk import Address, AddressError, Endpoint, EndpointScope, Manifest, parse_address

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5_000

_SCHEMA_V1 = """
CREATE TABLE plugin_endpoints (
  plugin_id        TEXT    NOT NULL,
  endpoint_id      TEXT    NOT NULL,
  address          TEXT    NOT NULL,
  endpoints_digest TEXT    NOT NULL,
  confirmed_at     INTEGER NOT NULL,
  PRIMARY KEY (plugin_id, endpoint_id)
);
CREATE TABLE plugin_network (
  plugin_id        TEXT    PRIMARY KEY,
  hosts            TEXT    NOT NULL,
  network_digest   TEXT    NOT NULL,
  approved_at      INTEGER NOT NULL
);
CREATE TABLE permission_audit (
  id               INTEGER PRIMARY KEY,
  plugin_id        TEXT    NOT NULL,
  target           TEXT    NOT NULL,
  action           TEXT    NOT NULL,
  old_value        TEXT,
  new_value        TEXT,
  digest           TEXT    NOT NULL,
  at               INTEGER NOT NULL
);
CREATE INDEX permission_audit_plugin ON permission_audit(plugin_id, id);
"""

_SCHEMA_V2 = """
CREATE TABLE data_source_config (
  category         TEXT    PRIMARY KEY,
  primary_plugin   TEXT    NOT NULL,
  backup_plugin    TEXT,
  updated_at       INTEGER NOT NULL
);
"""
"""v2（#99）：每个数据类别的主、备数据源，见 docs/subsystems/data-sources.md 的“配置”。
只新增表，不改 v1 的表；读写见 :mod:`duohelm.data_source_config`。"""

EndpointState = Literal["default", "confirmed", "needs_confirmation"]
NetworkState = Literal["none", "approved", "needs_approval"]
"""none：清单没有声明 network；approved：用户确认过当前列表；needs_approval：从没确认过，
或列表变了，确认之前插件不能联网。"""
AuditAction = Literal["confirm", "clear", "approve_network", "revoke_network"]
NETWORK_TARGET = "network"


class EndpointConfigError(Exception):
    """配置不成立。消息可直接展示给用户。"""


class UnknownEndpointError(EndpointConfigError):
    def __init__(self, plugin_id: str, endpoint_id: str) -> None:
        super().__init__(f"插件 {plugin_id} 的清单里没有声明 endpoint {endpoint_id!r}")


class ConfirmationRequiredError(EndpointConfigError):
    """保存前没有确认，或 internet 范围没有额外确认。"""


class StaleConfirmationError(ConfirmationRequiredError):
    """用户确认时看到的内容（按哈希）和服务器上的清单不一致：清单在这期间变了。"""

    def __init__(self) -> None:
        super().__init__("插件清单在你确认之前变了，请刷新页面，核对后重新确认")


def _check_expected(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise StaleConfirmationError


def endpoints_digest(manifest: Manifest) -> str:
    """只对清单里 ``[[permissions.endpoints]]`` 这一节算的哈希（规范化 JSON 的 sha256）。"""
    section = [e.model_dump(mode="json") for e in manifest.permissions.endpoints]
    text = json.dumps(section, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def network_digest(manifest: Manifest) -> str:
    """清单 ``permissions.network`` 列表的哈希（排序后的规范化 JSON 的 sha256）。"""
    hosts = sorted(manifest.permissions.network)
    text = json.dumps(hosts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checked_network(manifest: Manifest, network: Sequence[str]) -> list[str]:
    """传给子进程的 network 白名单必须是清单声明的子集（启动子进程前检查）。"""
    extra = sorted(set(network) - set(manifest.permissions.network))
    if extra:
        raise EndpointConfigError(
            f"插件 {manifest.id} 要放行的网络地址有 {len(extra)} 个不在清单的 network 里"
        )
    return list(network)


def checked_addresses(manifest: Manifest, addresses: Mapping[str, str]) -> dict[str, Address]:
    """把要传给插件的地址逐个按 scope 校验（启动子进程前的第二次校验）。

    只校验并返回 ``addresses`` 里的（用户确认过的）endpoint；没给的不补 default，插件拿不到
    它的地址。清单里没有声明的 id 直接报错。
    """
    declared = {e.id: e for e in manifest.permissions.endpoints}
    unknown = sorted(set(addresses) - set(declared))
    if unknown:
        raise UnknownEndpointError(manifest.id, ", ".join(unknown))
    result: dict[str, Address] = {}
    for endpoint_id, text in addresses.items():
        endpoint = declared[endpoint_id]
        try:
            result[endpoint_id] = parse_address(text, endpoint.scope)
        except AddressError:
            # 这条消息会进任务日志：只写 endpoint 和 scope，不写地址
            raise EndpointConfigError(
                f"插件 {manifest.id} 的 endpoint {endpoint_id!r} 的地址"
                f"不符合 {endpoint.scope} 范围，请在插件页重新设置"
            ) from None
    return result


@dataclass(frozen=True)
class EndpointView:
    """一个 endpoint 当前的配置，给 API 和页面用。"""

    plugin_id: str
    endpoint: Endpoint
    state: EndpointState
    configured: str | None
    """用户保存过的地址（需要重新确认时也返回，方便用户核对）。"""
    effective: str
    """state 为 confirmed 时是确认过的地址，插件的 ``endpoint()`` 返回它，也只有它放行；
    其余状态下是清单的 default，只用来在页面上预先填好，插件拿不到它（``endpoint()``
    报错），也连不上。"""
    confirmed_at: datetime | None
    digest: str
    """当前清单 endpoints 那一节的哈希。确认时传回来，防止确认的不是页面上看到的内容。"""


@dataclass(frozen=True)
class NetworkView:
    """清单 network 列表的确认状态，给 API 和页面用。"""

    plugin_id: str
    hosts: tuple[str, ...]
    """清单当前声明的列表。"""
    state: NetworkState
    approved_hosts: tuple[str, ...] | None
    """用户上次确认的列表（需要重新确认时也返回，方便对比）。"""
    approved_at: datetime | None
    digest: str
    """当前清单 network 列表的哈希。确认时传回来，防止确认的不是页面上看到的列表。"""


@dataclass(frozen=True)
class AuditEntry:
    id: int
    plugin_id: str
    target: str
    """``endpoint:<id>`` 或 ``network``。"""
    action: AuditAction
    old_value: str | None
    new_value: str | None
    digest: str
    """当时 endpoints 那一节（或 network 列表）的哈希。"""
    at: datetime


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def migrate_plugin_config(conn: sqlite3.Connection, path: Path) -> None:
    """把 ``plugin-config.db`` 升级到当前版本。在调用方的事务里执行；每一版只新增表。"""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"{path} 的版本 {version} 比当前平台支持的 {SCHEMA_VERSION} 新")
    for since, schema in ((1, _SCHEMA_V1), (2, _SCHEMA_V2)):
        if version < since:
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class EndpointStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._transaction() as conn:
            migrate_plugin_config(conn, self._path)

    # ---- 读 ----

    def views(self, manifest: Manifest) -> list[EndpointView]:
        digest = endpoints_digest(manifest)
        with self._connect() as conn:
            rows = {
                r["endpoint_id"]: r
                for r in conn.execute(
                    "SELECT * FROM plugin_endpoints WHERE plugin_id = ?", (manifest.id,)
                )
            }
        views: list[EndpointView] = []
        for endpoint in manifest.permissions.endpoints:
            row = rows.get(endpoint.id)
            if row is None:
                views.append(
                    EndpointView(
                        manifest.id, endpoint, "default", None, endpoint.default, None, digest
                    )
                )
                continue
            confirmed = row["endpoints_digest"] == digest
            views.append(
                EndpointView(
                    plugin_id=manifest.id,
                    endpoint=endpoint,
                    state="confirmed" if confirmed else "needs_confirmation",
                    configured=row["address"],
                    effective=row["address"] if confirmed else endpoint.default,
                    confirmed_at=_to_datetime(row["confirmed_at"]),
                    digest=digest,
                )
            )
        return views

    def addresses_for(self, manifest: Manifest) -> dict[str, str]:
        """用户确认过、并且仍然有效的地址（endpoint id -> 地址文本）。这些地址会放行。

        没确认过或需要重新确认的 endpoint 不在结果里：插件拿不到地址（``endpoint()``
        报错），也不放行。传给子进程之前还要经过 :func:`checked_addresses`。
        """
        return {v.endpoint.id: v.effective for v in self.views(manifest) if v.state == "confirmed"}

    def network_view(self, manifest: Manifest) -> NetworkView:
        hosts = tuple(manifest.permissions.network)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plugin_network WHERE plugin_id = ?", (manifest.id,)
            ).fetchone()
        approved = None if row is None else tuple(json.loads(row["hosts"]))
        approved_at = None if row is None else _to_datetime(row["approved_at"])
        if not hosts:
            state: NetworkState = "none"
        elif row is not None and row["network_digest"] == network_digest(manifest):
            state = "approved"
        else:
            state = "needs_approval"
        return NetworkView(
            manifest.id, hosts, state, approved, approved_at, network_digest(manifest)
        )

    def network_for(self, manifest: Manifest) -> list[str]:
        """要放行的 network 地址：用户确认过当前列表时是整个列表，否则为空（不能联网）。"""
        view = self.network_view(manifest)
        return list(view.hosts) if view.state == "approved" else []

    def audit(self, plugin_id: str) -> list[AuditEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM permission_audit WHERE plugin_id = ? ORDER BY id", (plugin_id,)
            ).fetchall()
        return [
            AuditEntry(
                id=r["id"],
                plugin_id=r["plugin_id"],
                target=r["target"],
                action=r["action"],
                old_value=r["old_value"],
                new_value=r["new_value"],
                digest=r["digest"],
                at=_to_datetime(r["at"]),
            )
            for r in rows
        ]

    # ---- 写 ----

    def confirm(
        self,
        manifest: Manifest,
        endpoint_id: str,
        address: str,
        *,
        confirmed: bool,
        confirm_internet: bool = False,
        expected_digest: str | None = None,
    ) -> EndpointView:
        """保存并确认地址（保存时的第一次校验）。同一个地址再确认一次，即“重新确认”。

        ``expected_digest``：用户确认时页面上 endpoints 那一节的哈希；和当前清单对不上时拒绝。
        API 必须传；``None`` 只给调用方自己读了当前清单的场合（测试等）。"""
        endpoint = _declared(manifest, endpoint_id)
        if not confirmed:
            raise ConfirmationRequiredError("保存连接地址需要确认")
        _check_expected(expected_digest, endpoints_digest(manifest))
        try:
            parsed = parse_address(address, endpoint.scope)
        except AddressError as exc:
            raise EndpointConfigError(str(exc)) from None
        if endpoint.scope is EndpointScope.INTERNET and not confirm_internet:
            raise ConfirmationRequiredError(
                "这个 endpoint 允许连接公网地址，保存前需要额外确认（confirm_internet）"
            )
        digest = endpoints_digest(manifest)
        now = _now_ms()
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT address FROM plugin_endpoints WHERE plugin_id = ? AND endpoint_id = ?",
                (manifest.id, endpoint_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO plugin_endpoints
                  (plugin_id, endpoint_id, address, endpoints_digest, confirmed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (plugin_id, endpoint_id) DO UPDATE SET
                  address = excluded.address,
                  endpoints_digest = excluded.endpoints_digest,
                  confirmed_at = excluded.confirmed_at
                """,
                (manifest.id, endpoint_id, str(parsed), digest, now),
            )
            self._record(
                conn,
                manifest.id,
                f"endpoint:{endpoint_id}",
                "confirm",
                None if old is None else old["address"],
                str(parsed),
                digest,
                now,
            )
        logger.info("插件 %s 的 endpoint %s 已确认新地址", manifest.id, endpoint_id)
        return self._view(manifest, endpoint_id)

    def clear(self, manifest: Manifest, endpoint_id: str) -> EndpointView:
        """清除用户的地址，恢复为 default。"""
        _declared(manifest, endpoint_id)
        digest = endpoints_digest(manifest)
        now = _now_ms()
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT address FROM plugin_endpoints WHERE plugin_id = ? AND endpoint_id = ?",
                (manifest.id, endpoint_id),
            ).fetchone()
            if old is not None:
                conn.execute(
                    "DELETE FROM plugin_endpoints WHERE plugin_id = ? AND endpoint_id = ?",
                    (manifest.id, endpoint_id),
                )
                self._record(
                    conn,
                    manifest.id,
                    f"endpoint:{endpoint_id}",
                    "clear",
                    old["address"],
                    None,
                    digest,
                    now,
                )
        if old is not None:
            logger.info("插件 %s 的 endpoint %s 已恢复为默认地址", manifest.id, endpoint_id)
        return self._view(manifest, endpoint_id)

    def approve_network(
        self, manifest: Manifest, *, confirmed: bool, expected_digest: str | None = None
    ) -> NetworkView:
        """确认清单 network 的整个列表。列表变了以后再调用一次，即“重新确认”。

        ``expected_digest``：用户确认时页面上列表的哈希；和当前清单对不上时拒绝，防止确认一个
        没看过的列表。API 必须传；``None`` 只给调用方自己读了当前清单的场合（测试等）。"""
        hosts = list(manifest.permissions.network)
        if not hosts:
            raise EndpointConfigError(f"插件 {manifest.id} 的清单没有声明 network")
        if not confirmed:
            raise ConfirmationRequiredError("放行插件的网络地址需要确认")
        digest = network_digest(manifest)
        _check_expected(expected_digest, digest)
        now = _now_ms()
        new_value = json.dumps(hosts, ensure_ascii=False)
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT hosts FROM plugin_network WHERE plugin_id = ?", (manifest.id,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO plugin_network (plugin_id, hosts, network_digest, approved_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (plugin_id) DO UPDATE SET
                  hosts = excluded.hosts,
                  network_digest = excluded.network_digest,
                  approved_at = excluded.approved_at
                """,
                (manifest.id, new_value, digest, now),
            )
            self._record(
                conn,
                manifest.id,
                NETWORK_TARGET,
                "approve_network",
                None if old is None else old["hosts"],
                new_value,
                digest,
                now,
            )
        logger.info("插件 %s 的 network 列表（%d 个地址）已确认", manifest.id, len(hosts))
        return self.network_view(manifest)

    def revoke_network(self, manifest: Manifest) -> NetworkView:
        """撤销 network 的确认：插件不能再联网，直到用户重新确认。"""
        now = _now_ms()
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT hosts, network_digest FROM plugin_network WHERE plugin_id = ?",
                (manifest.id,),
            ).fetchone()
            if old is not None:
                conn.execute("DELETE FROM plugin_network WHERE plugin_id = ?", (manifest.id,))
                self._record(
                    conn,
                    manifest.id,
                    NETWORK_TARGET,
                    "revoke_network",
                    old["hosts"],
                    None,
                    old["network_digest"],
                    now,
                )
        if old is not None:
            logger.info("插件 %s 的 network 确认已撤销", manifest.id)
        return self.network_view(manifest)

    def _view(self, manifest: Manifest, endpoint_id: str) -> EndpointView:
        return next(v for v in self.views(manifest) if v.endpoint.id == endpoint_id)

    @staticmethod
    def _record(  # noqa: PLR0917  审计记录的各列
        conn: sqlite3.Connection,
        plugin_id: str,
        target: str,
        action: AuditAction,
        old_value: str | None,
        new_value: str | None,
        digest: str,
        at: int,
    ) -> None:
        conn.execute(
            "INSERT INTO permission_audit "
            "(plugin_id, target, action, old_value, new_value, digest, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plugin_id, target, action, old_value, new_value, digest, at),
        )


def _declared(manifest: Manifest, endpoint_id: str) -> Endpoint:
    for endpoint in manifest.permissions.endpoints:
        if endpoint.id == endpoint_id:
            return endpoint
    raise UnknownEndpointError(manifest.id, endpoint_id)
