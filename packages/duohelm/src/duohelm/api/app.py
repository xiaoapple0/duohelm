"""FastAPI 应用。Web 前端只通过这里访问平台能力。"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from duohelm import __version__
from duohelm.registry import PluginEntry, PluginRegistry, PluginStatus
from duohelm.settings import Settings
from duohelm_sdk import SDK_VERSION, PluginKind


class HealthResponse(BaseModel):
    status: str
    version: str
    sdk_version: str


class PluginOut(BaseModel):
    id: str
    status: PluginStatus
    name: str | None
    version: str | None
    kind: PluginKind | None
    path: str
    problems: list[str]

    @classmethod
    def from_entry(cls, entry: PluginEntry) -> PluginOut:
        info = entry.manifest.plugin if entry.manifest else None
        return cls(
            id=entry.id,
            status=entry.status,
            name=info.name if info else None,
            version=info.version if info else None,
            kind=info.kind if info else None,
            path=str(entry.path),
            problems=list(entry.problems),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    registry = PluginRegistry([settings.plugin_dir])
    app = FastAPI(title="DuoHelm", version=__version__)

    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, sdk_version=SDK_VERSION)

    def list_plugins() -> list[PluginOut]:
        return [PluginOut.from_entry(e) for e in registry.scan()]

    app.add_api_route("/api/health", health, methods=["GET"], response_model=HealthResponse)
    app.add_api_route("/api/plugins", list_plugins, methods=["GET"], response_model=list[PluginOut])
    return app
