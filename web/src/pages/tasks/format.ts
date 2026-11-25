import type { Task } from "../../api/client";
import { TEXT } from "./text";

export function formatTime(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "-";
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return TEXT.duration.hours(h, m);
  }
  if (m > 0) {
    return TEXT.duration.minutes(m, s);
  }
  return TEXT.duration.seconds(s);
}

/** 本次执行的耗时：运行中算到 now，已结束算到 finished_at，排队中没有耗时。 */
export function taskDuration(task: Task, now: number): string {
  if (!task.started_at) {
    return "-";
  }
  const start = Date.parse(task.started_at);
  if (task.status === "running") {
    return formatDuration(now - start);
  }
  if (task.finished_at) {
    return formatDuration(Date.parse(task.finished_at) - start);
  }
  return "-";
}

export function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
