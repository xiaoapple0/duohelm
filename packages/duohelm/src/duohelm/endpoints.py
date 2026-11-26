"""用户配置的连接地址（插件清单里的 ``[[permissions.endpoints]]``）：保存、确认、取用。

规则见 docs/subsystems/plugins.md 的“用户可配置的连接地址”：

- 地址由用户在平台里填写并确认后才保存，保存时按 endpoint 的 scope 校验（第一次校验）
- 启动插件子进程、传地址之前，:class:`duohelm.isolation.runner.PluginSession` 再按 scope
  校验一次（第二次校验，见 :func:`checked_addresses`）
- 是否需要重新确认只看清单里 endpoints 那一节的哈希（:func:`endpoints_digest`）：插件升版本、
  改描述等清单其他部分的改动不需要重新确认；这一节变了，已保存的地址在重新确认之前不使用，
  插件只能用清单的 default
- 每次确认、清除都记审计日志（只在本机数据库里）。普通日志不写具体地址

地址保存在 ``DUOHELM_HOME/plugin-config.db``，不进任何仓库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from duohelm_sdk import Address, AddressError, Endpoint, EndpointScope, Manifest, parse_address

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
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
CREATE TABLE endpoint_audit (
  id               INTEGER PRIMARY KEY,
  plugin_id        TEXT    NOT NULL,
  endpoint_id      TEXT    NOT NULL,
  action           TEXT    NOT NULL,
  old_address      TEXT,
  new_address      TEXT,
  endpoints_digest TEXT    NOT NULL,
  at               INTEGER NOT NULL
);
CREATE INDEX endpoint_audit_plugin ON endpoint_audit(plugin_id, id);
"""

EndpointState = Literal["default", "confirmed", "needs_confirmation"]
AuditAction = Literal["confirm", "clear"]


class EndpointConfigError(Exception):
    """配置不成立。消息可直接展示给用户。"""


class UnknownEndpointError(EndpointConfigError):
    def __init__(self, plugin_id: str, endpoint_id: str) -> None:
        super().__init__(f"插件 {plugin_id} 的清单里没有声明 endpoint {endpoint_id!r}")


class ConfirmationRequiredError(EndpointConfigError):
    """保存前没有确认，或 internet 范围没有额外确认。"""


def endpoints_digest(manifest: Manifest) -> str:
    """只对清单里 ``[[permissions.endpoints]]`` 这一节算的哈希（规范化 JSON 的 sha256）。"""
    section = [e.model_dump(mode="json") for e in manifest.permissions.endpoints]
    text = json.dumps(section, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checked_addresses(manifest: Manifest, addresses: Mapping[str, str]) -> dict[str, Address]:
    """把要传给插件的地址逐个按 scope 校验（启动子进程前的第二次校验）。

    ``addresses`` 里没有的 endpoint 用清单的 default；清单里没有声明的 id 直接报错。
    """
    declared = {e.id: e for e in manifest.permissions.endpoints}
    unknown = sorted(set(addresses) - set(declared))
    if unknown:
        raise UnknownEndpointError(manifest.id, ", ".join(unknown))
    result: dict[str, Address] = {}
    for endpoint_id, endpoint in declared.items():
        text = addresses.get(endpoint_id, endpoint.default)
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
    """插件现在会拿到的地址：已确认的地址，否则是 default。"""
    confirmed_at: datetime | None


@dataclass(frozen=True)
class AuditEntry:
    id: int
    plugin_id: str
    endpoint_id: str
    action: AuditAction
    old_address: str | None
    new_address: str | None
    endpoints_digest: str
    at: datetime


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


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
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self._path} 的版本 {version} 比当前平台支持的 {SCHEMA_VERSION} 新"
                )
            if version < 1:
                for statement in _SCHEMA_V1.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
                    EndpointView(manifest.id, endpoint, "default", None, endpoint.default, None)
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
                )
            )
        return views

    def addresses_for(self, manifest: Manifest) -> dict[str, str]:
        """要传给插件的地址文本：已确认的用用户的，其余（含需要重新确认的）用 default。

        传给子进程之前还要经过 :func:`checked_addresses`。
        """
        return {v.endpoint.id: v.effective for v in self.views(manifest)}

    def audit(self, plugin_id: str) -> list[AuditEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM endpoint_audit WHERE plugin_id = ? ORDER BY id", (plugin_id,)
            ).fetchall()
        return [
            AuditEntry(
                id=r["id"],
                plugin_id=r["plugin_id"],
                endpoint_id=r["endpoint_id"],
                action=r["action"],
                old_address=r["old_address"],
                new_address=r["new_address"],
                endpoints_digest=r["endpoints_digest"],
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
    ) -> EndpointView:
        """保存并确认地址（保存时的第一次校验）。同一个地址再确认一次，即“重新确认”。"""
        endpoint = _declared(manifest, endpoint_id)
        if not confirmed:
            raise ConfirmationRequiredError("保存连接地址需要确认")
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
            self._record(conn, manifest.id, endpoint_id, "confirm", old, str(parsed), digest, now)
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
                self._record(conn, manifest.id, endpoint_id, "clear", old, None, digest, now)
        if old is not None:
            logger.info("插件 %s 的 endpoint %s 已恢复为默认地址", manifest.id, endpoint_id)
        return self._view(manifest, endpoint_id)

    def _view(self, manifest: Manifest, endpoint_id: str) -> EndpointView:
        return next(v for v in self.views(manifest) if v.endpoint.id == endpoint_id)

    @staticmethod
    def _record(  # noqa: PLR0917  审计记录的各列
        conn: sqlite3.Connection,
        plugin_id: str,
        endpoint_id: str,
        action: AuditAction,
        old: sqlite3.Row | None,
        new_address: str | None,
        digest: str,
        at: int,
    ) -> None:
        conn.execute(
            "INSERT INTO endpoint_audit "
            "(plugin_id, endpoint_id, action, old_address, new_address, endpoints_digest, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                plugin_id,
                endpoint_id,
                action,
                None if old is None else old["address"],
                new_address,
                digest,
                at,
            ),
        )


def _declared(manifest: Manifest, endpoint_id: str) -> Endpoint:
    for endpoint in manifest.permissions.endpoints:
        if endpoint.id == endpoint_id:
            return endpoint
    raise UnknownEndpointError(manifest.id, endpoint_id)
