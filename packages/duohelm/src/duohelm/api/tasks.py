"""任务系统的 HTTP 接口：/api/tasks、/api/task-types。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from duohelm.tasks import (
    DuplicateTaskError,
    InvalidTransitionError,
    TaskLog,
    TaskNotFoundError,
    TaskRecord,
    TaskStatus,
    TaskStore,
    TaskTypeRegistry,
    UnknownTaskTypeError,
)
from duohelm.tasks.store import LogLevel


class TaskOut(BaseModel):
    id: int
    type: str
    params: dict[str, Any]
    status: TaskStatus
    progress: float | None
    progress_message: str | None
    attempt: int
    max_attempts: int
    run_after: datetime
    cancel_requested: bool
    error: str | None
    result: dict[str, Any] | None
    dedupe_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_record(cls, task: TaskRecord) -> TaskOut:
        return cls(
            id=task.id,
            type=task.type,
            params=json.loads(task.params),
            status=task.status,
            progress=task.progress,
            progress_message=task.progress_message,
            attempt=task.attempt,
            max_attempts=task.max_attempts,
            run_after=task.run_after,
            cancel_requested=task.cancel_requested,
            error=task.error,
            result=json.loads(task.result) if task.result is not None else None,
            dedupe_key=task.dedupe_key,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )


class TaskLogOut(BaseModel):
    id: int
    attempt: int
    ts: datetime
    level: LogLevel
    message: str

    @classmethod
    def from_log(cls, log: TaskLog) -> TaskLogOut:
        return cls(id=log.id, attempt=log.attempt, ts=log.ts, level=log.level, message=log.message)


class TaskTypeOut(BaseModel):
    name: str
    description: str
    max_attempts: int
    params_schema: dict[str, Any]


class TaskCreate(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = None


def task_router(store: TaskStore, types: TaskTypeRegistry) -> APIRouter:
    router = APIRouter()

    def _get(task_id: int) -> TaskRecord:
        try:
            return store.get(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    def list_task_types() -> list[TaskTypeOut]:
        return [
            TaskTypeOut(
                name=t.name,
                description=t.description,
                max_attempts=t.max_attempts,
                params_schema=t.params_model.model_json_schema(),
            )
            for t in types.all()
        ]

    def list_tasks(
        status: TaskStatus | None = None,
        type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_id: int | None = None,
    ) -> list[TaskOut]:
        records = store.list(status=status, type=type, limit=limit, before_id=before_id)
        return [TaskOut.from_record(t) for t in records]

    def create_task(body: TaskCreate) -> TaskOut:
        try:
            params = types.validate_params(body.type, body.params)
        except UnknownTaskTypeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False))) from exc
        max_attempts = types.get(body.type).max_attempts
        try:
            task = store.create(
                body.type, params, max_attempts=max_attempts, dedupe_key=body.dedupe_key
            )
        except DuplicateTaskError as exc:
            raise HTTPException(409, str(exc)) from exc
        return TaskOut.from_record(task)

    def get_task(task_id: int) -> TaskOut:
        return TaskOut.from_record(_get(task_id))

    def get_logs(
        task_id: int,
        after_id: int = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> list[TaskLogOut]:
        try:
            logs = store.logs(task_id, after_id=after_id, limit=limit)
        except TaskNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return [TaskLogOut.from_log(log) for log in logs]

    def cancel_task(task_id: int) -> TaskOut:
        _get(task_id)
        try:
            return TaskOut.from_record(store.request_cancel(task_id))
        except InvalidTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc

    def retry_task(task_id: int) -> TaskOut:
        task = _get(task_id)
        try:
            extra = types.get(task.type).max_attempts
        except UnknownTaskTypeError as exc:
            raise HTTPException(409, f"{exc}，无法重试") from exc
        try:
            return TaskOut.from_record(store.retry(task_id, extra_attempts=extra))
        except (InvalidTransitionError, DuplicateTaskError) as exc:
            raise HTTPException(409, str(exc)) from exc

    router.add_api_route(
        "/api/task-types", list_task_types, methods=["GET"], response_model=list[TaskTypeOut]
    )
    router.add_api_route("/api/tasks", list_tasks, methods=["GET"], response_model=list[TaskOut])
    router.add_api_route(
        "/api/tasks", create_task, methods=["POST"], response_model=TaskOut, status_code=201
    )
    router.add_api_route("/api/tasks/{task_id}", get_task, methods=["GET"], response_model=TaskOut)
    router.add_api_route(
        "/api/tasks/{task_id}/logs", get_logs, methods=["GET"], response_model=list[TaskLogOut]
    )
    router.add_api_route(
        "/api/tasks/{task_id}/cancel", cancel_task, methods=["POST"], response_model=TaskOut
    )
    router.add_api_route(
        "/api/tasks/{task_id}/retry", retry_task, methods=["POST"], response_model=TaskOut
    )
    return router
