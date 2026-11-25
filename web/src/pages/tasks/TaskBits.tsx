import { Button, Popconfirm, Progress, Space, Tag, Typography } from "antd";
import { useState } from "react";

import { api, isUnfinished, type Task, type TaskStatus } from "../../api/client";
import { formatTime } from "./format";
import { TEXT } from "./text";

const STATUS_COLOR: Record<TaskStatus, string> = {
  queued: "default",
  running: "processing",
  succeeded: "success",
  failed: "error",
  cancelled: "warning",
};

export function StatusTag({ task }: { task: Task }) {
  if (task.status === "running" && task.cancel_requested) {
    return <Tag color="warning">{TEXT.cancelling}</Tag>;
  }
  return <Tag color={STATUS_COLOR[task.status]}>{TEXT.status[task.status]}</Tag>;
}

/** 排队中、但因为之前失败在等待退避的任务，提示下次重试的时间。 */
export function RetryHint({ task }: { task: Task }) {
  if (task.status !== "queued" || task.attempt === 0) {
    return null;
  }
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      {TEXT.waitingRetry(formatTime(task.run_after))}
    </Typography.Text>
  );
}

export function TaskProgress({ task }: { task: Task }) {
  if (task.progress === null && task.status !== "succeeded") {
    return task.progress_message ? (
      <Typography.Text type="secondary">{task.progress_message}</Typography.Text>
    ) : (
      <span>-</span>
    );
  }
  const percent = Math.round((task.progress ?? 1) * 100);
  const status =
    task.status === "failed"
      ? "exception"
      : task.status === "succeeded"
        ? "success"
        : task.status === "running"
          ? "active"
          : "normal";
  return (
    <div style={{ minWidth: 140 }}>
      <Progress percent={percent} size="small" status={status} />
      {task.progress_message && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {task.progress_message}
        </Typography.Text>
      )}
    </div>
  );
}

interface ActionsProps {
  task: Task;
  onDone: (task: Task, message: string) => void;
  onError: (message: string) => void;
  size?: "small" | "middle";
}

/** 取消（二次确认）和重试按钮。按钮是否出现由任务状态决定。 */
export function TaskActions({ task, onDone, onError, size = "small" }: ActionsProps) {
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<Task>, message: (id: number) => string) {
    setBusy(true);
    try {
      const updated = await action();
      onDone(updated, message(updated.id));
    } catch (err: unknown) {
      onError(TEXT.action.failed(err instanceof Error ? err.message : String(err)));
    } finally {
      setBusy(false);
    }
  }

  const canCancel = isUnfinished(task.status) && !task.cancel_requested;
  const canRetry = task.status === "failed" || task.status === "cancelled";

  return (
    <Space size="small">
      {canCancel && (
        <Popconfirm
          title={TEXT.action.cancelConfirmTitle}
          description={
            task.status === "queued"
              ? TEXT.action.cancelConfirmQueued
              : TEXT.action.cancelConfirmRunning
          }
          okText={TEXT.action.cancelConfirmOk}
          cancelText={TEXT.action.cancelConfirmBack}
          okButtonProps={{ danger: true }}
          onConfirm={() => run(() => api.cancelTask(task.id), TEXT.action.cancelled)}
        >
          <Button size={size} danger loading={busy}>
            {TEXT.action.cancel}
          </Button>
        </Popconfirm>
      )}
      {canRetry && (
        <Button
          size={size}
          loading={busy}
          onClick={() => run(() => api.retryTask(task.id), TEXT.action.retried)}
        >
          {TEXT.action.retry}
        </Button>
      )}
    </Space>
  );
}
