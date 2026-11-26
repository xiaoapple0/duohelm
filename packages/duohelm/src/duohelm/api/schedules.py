"""定时规则的 HTTP 接口：/api/schedules/data-sync。设计见 docs/subsystems/tasks.md。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from duohelm.registry import PluginEntry, PluginStatus
from duohelm.tasks.schedule import (
    ScheduleError,
    Scheduler,
    ScheduleStore,
    parse_run_time,
)

TIMEZONE = "Asia/Shanghai"


class ScheduleOut(BaseModel):
    enabled: bool
    plugin_id: str | None
    time: str
    """每个交易日触发的时刻，HH:MM，北京时间。"""
    timezone: str
    last_trigger_date: date | None
    plugin_warning: str | None
    """启用时插件状态不是 ready 的说明；仍然保存，触发时 data.sync 会失败。"""
    needs_full_sync: bool | None
    """已启用，但当前快照没有日线或日历：不会自动触发，需要先手动做一次全量同步。
    读当前快照失败时为 null（未知），原因见 state_error。"""
    state_error: str | None
    """读当前快照失败的说明；正常时为 null。"""
    next_run_at: datetime | None
    """下次触发时间（带 +08:00 偏移）。未启用或读快照失败（未知）时为 null；
    日历没覆盖的日子只按周末判断，是预估。"""


class SchedulePatch(BaseModel):
    enabled: bool | None = None
    plugin_id: str | None = None
    time: str | None = None


def schedule_router(
    store: ScheduleStore, scheduler: Scheduler, scan: Callable[[], list[PluginEntry]]
) -> APIRouter:
    router = APIRouter()

    def plugin_warning(plugin_id: str | None, enabled: bool) -> str | None:
        if not enabled or not plugin_id:
            return None
        entry = next((e for e in scan() if e.id == plugin_id), None)
        if entry is None:
            return f"找不到插件 {plugin_id}（可能已被删除），定时同步触发时会失败"
        if entry.status is PluginStatus.READY:
            return None
        return f"插件 {plugin_id} 的状态是 {entry.status}，定时同步触发时会失败"

    def check_plugin(plugin_id: str) -> None:
        entry = next((e for e in scan() if e.id == plugin_id), None)
        if entry is None:
            raise ScheduleError(f"找不到插件 {plugin_id}")
        if entry.manifest is not None and entry.manifest.plugin.kind != "datasource":
            raise ScheduleError(
                f"插件 {plugin_id} 不是行情源（kind 为 {entry.manifest.plugin.kind}）"
            )

    def out() -> ScheduleOut:
        cfg = store.get()
        status = scheduler.status()
        return ScheduleOut(
            enabled=cfg.enabled,
            plugin_id=cfg.plugin_id,
            time=f"{cfg.run_time:%H:%M}",
            timezone=TIMEZONE,
            last_trigger_date=cfg.last_trigger_date,
            plugin_warning=plugin_warning(cfg.plugin_id, cfg.enabled),
            needs_full_sync=status.needs_full_sync,
            state_error=status.state_error,
            next_run_at=status.next_run_at,
        )

    def get_schedule() -> ScheduleOut:
        return out()

    def patch_schedule(body: SchedulePatch) -> ScheduleOut:
        try:
            current = store.get()
            effective = body.plugin_id.strip() if body.plugin_id is not None else current.plugin_id
            will_enable = current.enabled if body.enabled is None else body.enabled
            if will_enable and effective:
                check_plugin(effective)
            store.update(
                now=scheduler.now(),
                enabled=body.enabled,
                plugin_id=body.plugin_id,
                run_time=parse_run_time(body.time) if body.time is not None else None,
            )
        except ScheduleError as exc:
            raise HTTPException(422, str(exc)) from exc
        return out()

    router.add_api_route(
        "/api/schedules/data-sync", get_schedule, methods=["GET"], response_model=ScheduleOut
    )
    router.add_api_route(
        "/api/schedules/data-sync", patch_schedule, methods=["PATCH"], response_model=ScheduleOut
    )
    return router
