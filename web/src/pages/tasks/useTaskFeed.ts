import { useCallback, useEffect, useRef, useState } from "react";

import { api, isUnfinished, type Task, type TaskLog } from "../../api/client";
import { usePolling } from "../../api/usePolling";

const POLL_MS = 2000;

interface TaskFeed {
  task: Task | undefined;
  logs: TaskLog[];
  error: string | undefined;
  refresh: () => Promise<void>;
  setTask: (task: Task) => void;
}

/** 跟踪一个任务：取任务和增量日志，任务没结束时每 2 秒刷新。taskId 变化时清空重来。 */
export function useTaskFeed(taskId: number | undefined): TaskFeed {
  const [task, setTask] = useState<Task>();
  const [logs, setLogs] = useState<TaskLog[]>([]);
  const [error, setError] = useState<string>();
  const lastLogId = useRef(0);
  const currentId = useRef(taskId);
  currentId.current = taskId;

  const refresh = useCallback(async () => {
    if (taskId === undefined) {
      return;
    }
    try {
      // 先取任务再取日志：任务已结束时，随后取到的日志一定包含最后几行
      const fresh = await api.task(taskId);
      const more = await api.taskLogs(taskId, lastLogId.current);
      if (currentId.current !== taskId) {
        return;
      }
      setTask(fresh);
      const last = more.at(-1);
      if (last) {
        lastLogId.current = Math.max(lastLogId.current, last.id);
        // 两次刷新可能并发（轮询和按钮操作），按 id 去重，只追加新行
        setLogs((prev) => {
          const seen = prev.at(-1)?.id ?? 0;
          return [...prev, ...more.filter((log) => log.id > seen)];
        });
      }
      setError(undefined);
    } catch (err: unknown) {
      if (currentId.current === taskId) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }, [taskId]);

  useEffect(() => {
    setTask(undefined);
    setLogs([]);
    setError(undefined);
    lastLogId.current = 0;
    void refresh();
  }, [refresh]);

  usePolling(refresh, POLL_MS, task !== undefined && isUnfinished(task.status));

  return { task, logs, error, refresh, setTask };
}
