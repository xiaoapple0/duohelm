"""插件的 HTTP 接口：/api/plugins，以及用户配置的连接地址 /api/plugins/{id}/endpoints。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from duohelm.endpoints import (
    ConfirmationRequiredError,
    EndpointConfigError,
    EndpointState,
    EndpointStore,
    EndpointView,
    UnknownEndpointError,
)
from duohelm.registry import PluginEntry, PluginRegistry, PluginStatus
from duohelm_sdk import EndpointScope, Manifest, PluginKind


class EndpointOut(BaseModel):
    id: str
    description: str
    scope: EndpointScope
    default: str
    state: EndpointState
    """default：没有配置；confirmed：用已确认的地址；needs_confirmation：清单的 endpoints
    那一节变了，已保存的地址在重新确认之前不使用。"""
    configured: str | None
    effective: str
    confirmed_at: datetime | None

    @classmethod
    def from_view(cls, view: EndpointView) -> EndpointOut:
        e = view.endpoint
        return cls(
            id=e.id,
            description=e.description,
            scope=e.scope,
            default=e.default,
            state=view.state,
            configured=view.configured,
            effective=view.effective,
            confirmed_at=view.confirmed_at,
        )


class PluginOut(BaseModel):
    id: str
    status: PluginStatus
    name: str | None
    version: str | None
    kind: PluginKind | None
    path: str
    problems: list[str]
    endpoints: list[EndpointOut]

    @classmethod
    def from_entry(cls, entry: PluginEntry, store: EndpointStore) -> PluginOut:
        m = entry.manifest
        info = m.plugin if m else None
        return cls(
            id=entry.id,
            status=entry.status,
            name=info.name if info else None,
            version=info.version if info else None,
            kind=info.kind if info else None,
            path=str(entry.path),
            problems=list(entry.problems),
            endpoints=[EndpointOut.from_view(v) for v in store.views(m)] if m else [],
        )


class EndpointConfirm(BaseModel):
    address: str
    """``主机:端口``，IPv6 写成 ``[地址]:端口``。"""
    confirm: Literal[True]
    """必须为 true：用户已经看过“插件将可以连接这个地址”的提示并确认。"""
    confirm_internet: bool = False
    """scope 为 internet 时必须为 true（额外确认）。"""


def plugin_router(registry: PluginRegistry, store: EndpointStore) -> APIRouter:
    router = APIRouter(prefix="/api/plugins", tags=["plugins"])

    def manifest_of(plugin_id: str) -> Manifest:
        for entry in registry.scan():
            if entry.id == plugin_id and entry.manifest is not None:
                return entry.manifest
        raise HTTPException(404, f"插件 {plugin_id} 不存在，或清单无效")

    def config_error(exc: EndpointConfigError) -> HTTPException:
        if isinstance(exc, UnknownEndpointError):
            return HTTPException(404, str(exc))
        if isinstance(exc, ConfirmationRequiredError):
            return HTTPException(409, str(exc))
        return HTTPException(422, str(exc))

    @router.get("", response_model=list[PluginOut])
    def list_plugins() -> list[PluginOut]:
        return [PluginOut.from_entry(e, store) for e in registry.scan()]

    @router.get("/{plugin_id}/endpoints", response_model=list[EndpointOut])
    def list_endpoints(plugin_id: str) -> list[EndpointOut]:
        return [EndpointOut.from_view(v) for v in store.views(manifest_of(plugin_id))]

    @router.put("/{plugin_id}/endpoints/{endpoint_id}", response_model=EndpointOut)
    def confirm_endpoint(plugin_id: str, endpoint_id: str, body: EndpointConfirm) -> EndpointOut:
        manifest = manifest_of(plugin_id)
        try:
            view = store.confirm(
                manifest,
                endpoint_id,
                body.address,
                confirmed=body.confirm,
                confirm_internet=body.confirm_internet,
            )
        except EndpointConfigError as exc:
            raise config_error(exc) from None
        return EndpointOut.from_view(view)

    @router.delete("/{plugin_id}/endpoints/{endpoint_id}", response_model=EndpointOut)
    def clear_endpoint(plugin_id: str, endpoint_id: str) -> EndpointOut:
        try:
            view = store.clear(manifest_of(plugin_id), endpoint_id)
        except EndpointConfigError as exc:
            raise config_error(exc) from None
        return EndpointOut.from_view(view)

    return router
