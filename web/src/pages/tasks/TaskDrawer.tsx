import { Alert, Descriptions, Divider, Drawer, Empty, Spin, Tag, Typography } from "antd";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api, isUnfinished, type LogLevel, type Task, type TaskLog } from "../../api/client";
import { usePolling } from "../../api/usePolling";
import { formatJson, formatTime } from "./format";
import { RetryHint, StatusTag, TaskActions, TaskProgress } from "./TaskBits";
import { TEXT } from "./text";

const POLL_MS = 2000;

const LEVEL_COLOR: Record<LogLevel, string> = {
  debug: "default",
  info: "blue",
  warning: "orange",
  error: "red",
};

const preStyle = {
  margin: 0,
  padding: 8,
  maxHeight: 240,
  overflow: "auto",
  background: "rgba(0, 0, 0, 0.04)",
  borderRadius: 6,
  fontSize: 12,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
} as const;

interface Props {
  taskId: number | undefined;
  onClose: () => void;
  onChanged: (task: Task, message: string) => void;
  onError: (message: string) => void;
}

function groupByAttempt(logs: TaskLog[]): [number, TaskLog[]][] {
  const groups = new Map<number, TaskLog[]>();
  for (const log of logs) {
    const list = groups.get(log.attempt);
    if (list) {
      list.push(log);
    } else {
      groups.set(log.attempt, [log]);
    }
  }
  return [...groups.entries()];
}

export default function TaskDrawer({ taskId, onClose, onChanged, onError }: Props) {
  const [task, setTask] = useState<Task>();
  const [logs, setLogs] = useState<TaskLog[]>([]);
  const [error, setError] = useState<string>();
  const lastLogId = useRef(0);
  const logBox = useRef<HTMLDivElement>(null);
  const currentId = useRef(taskId);
  currentId.current = taskId;

  const refresh = useCallback(async () => {
    if (taskId === undefined) {
      return;
    }
    try {
      // 先取任务再取日志：任务已结束时，随后取到的日志一定包含最后几行。
      const fresh = await api.task(taskId);
      const more = await api.taskLogs(taskId, lastLogId.current);
      if (currentId.current !== taskId) {
        return; // 抽屉已切换到别的任务
      }
      setTask(fresh);
      const last = more.at(-1);
      if (last) {
        lastLogId.current = Math.max(lastLogId.current, last.id);
        // 两次刷新可能并发（轮询和按钮操作），按 id 去重，保证只追加新行
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

  // 运行中的任务有新日志时滚到最新一行
  useLayoutEffect(() => {
    const box = logBox.current;
    if (box && task && isUnfinished(task.status)) {
      box.scrollTop = box.scrollHeight;
    }
  }, [logs, task]);

  const handleChanged = (updated: Task, message: string) => {
    setTask(updated);
    onChanged(updated, message);
    void refresh();
  };

  return (
    <Drawer
      open={taskId !== undefined}
      onClose={onClose}
      width={640}
      title={task ? TEXT.drawer.title(task.id, task.type) : undefined}
      extra={task && <TaskActions task={task} onDone={handleChanged} onError={onError} size="middle" />}
      destroyOnHidden
    >
      {error && (
        <Alert type="error" showIcon message={TEXT.drawer.loadFailed} description={error} />
      )}
      {!task && !error && <Spin />}
      {task && (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label={TEXT.column.status}>
              <StatusTag task={task} /> <RetryHint task={task} />
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.column.attempt}>
              {task.attempt}/{task.max_attempts}
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.column.progress} span={2}>
              <TaskProgress task={task} />
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.drawer.createdAt}>
              {formatTime(task.created_at)}
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.drawer.startedAt}>
              {formatTime(task.started_at)}
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.drawer.finishedAt}>
              {formatTime(task.finished_at)}
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.drawer.dedupeKey}>
              {task.dedupe_key ?? TEXT.drawer.none}
            </Descriptions.Item>
          </Descriptions>

          <Divider orientation="left">{TEXT.drawer.params}</Divider>
          <pre style={preStyle}>{formatJson(task.params)}</pre>

          {task.error && (
            <>
              <Divider orientation="left">{TEXT.drawer.error}</Divider>
              <Alert type="error" message={task.error} />
            </>
          )}

          {task.result && (
            <>
              <Divider orientation="left">{TEXT.drawer.result}</Divider>
              <pre style={preStyle}>{formatJson(task.result)}</pre>
            </>
          )}

          <Divider orientation="left">{TEXT.drawer.logs}</Divider>
          <div ref={logBox} style={{ ...preStyle, maxHeight: 360, whiteSpace: "normal" }}>
            {logs.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={TEXT.drawer.noLogs} />
            ) : (
              groupByAttempt(logs).map(([attempt, items]) => (
                <div key={attempt} style={{ marginBottom: 12 }}>
                  <Typography.Text strong>{TEXT.drawer.attempt(attempt)}</Typography.Text>
                  {items.map((log) => (
                    <div
                      key={log.id}
                      style={{ fontFamily: "monospace", whiteSpace: "pre-wrap", marginTop: 2 }}
                    >
                      <Typography.Text type="secondary">{formatTime(log.ts)}</Typography.Text>{" "}
                      <Tag color={LEVEL_COLOR[log.level]} style={{ marginInlineEnd: 4 }}>
                        {TEXT.level[log.level]}
                      </Tag>
                      {log.message}
                    </div>
                  ))}
                </div>
              ))
            )}
          </div>
        </>
      )}
    </Drawer>
  );
}
