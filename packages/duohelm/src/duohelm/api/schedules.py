"""定时规则的 HTTP 接口：/api/schedules/data-sync。设计见 docs/subsystems/tasks.md。"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
    next_run_at: datetime | None
    """下次触发时间（带 +08:00 偏移）。未启用时为空；日历没覆盖的日子只按周末判断，是预估。"""


class SchedulePatch(BaseModel):
    enabled: bool | None = None
    plugin_id: str | None = None
    time: str | None = None


def schedule_router(store: ScheduleStore, scheduler: Scheduler) -> APIRouter:
    router = APIRouter()

    def out() -> ScheduleOut:
        cfg = store.get()
        return ScheduleOut(
            enabled=cfg.enabled,
            plugin_id=cfg.plugin_id,
            time=f"{cfg.run_time:%H:%M}",
            timezone=TIMEZONE,
            last_trigger_date=cfg.last_trigger_date,
            next_run_at=scheduler.next_run_at(),
        )

    def get_schedule() -> ScheduleOut:
        return out()

    def patch_schedule(body: SchedulePatch) -> ScheduleOut:
        try:
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
