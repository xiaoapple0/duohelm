import { Alert, App, Card, Descriptions, Divider, Empty, Spin, Tag, Typography } from "antd";
import { useLayoutEffect, useRef } from "react";

import { isUnfinished, type LogLevel, type Task, type TaskLog } from "../../api/client";
import { formatJson, formatTime } from "../tasks/format";
import { RetryHint, StatusTag, TaskActions, TaskProgress } from "../tasks/TaskBits";
import { TEXT as TASK_TEXT } from "../tasks/text";
import SyncResultView from "./SyncResultView";
import { TEXT } from "./text";
import { MANUAL_DEDUPE_KEY, SCHEDULE_DEDUPE_KEY } from "./keys";

const T = TEXT.task;

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

function source(task: Task): string {
  if (task.dedupe_key === MANUAL_DEDUPE_KEY) {
    return T.manual;
  }
  if (task.dedupe_key === SCHEDULE_DEDUPE_KEY) {
    return T.scheduled;
  }
  return T.other;
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

interface Props {
  taskId: number | undefined;
  task: Task | undefined;
  logs: TaskLog[];
  error: string | undefined;
  loading: boolean;
  onChanged: (task: Task) => void;
}

/** 最近一个同步任务的进度、日志、结果，以及取消和重试。数据由上层的 useTaskFeed 提供。 */
export default function TaskPanel({ taskId, task, logs, error, loading, onChanged }: Props) {
  const { message } = App.useApp();
  const logBox = useRef<HTMLDivElement>(null);

  // 运行中的任务有新日志时滚到最新一行
  useLayoutEffect(() => {
    const box = logBox.current;
    if (box && task && isUnfinished(task.status)) {
      box.scrollTop = box.scrollHeight;
    }
  }, [logs, task]);

  let body;
  if (taskId === undefined) {
    body = loading ? (
      <Spin />
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={T.none} />
    );
  } else {
    body = (
      <>
        {error && <Alert type="error" showIcon message={T.loadFailed} description={error} />}
        {!task && !error && <Spin />}
        {task && (
          <>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label={T.status}>
                <StatusTag task={task} /> <RetryHint task={task} />
              </Descriptions.Item>
              <Descriptions.Item label={T.source}>{source(task)}</Descriptions.Item>
              <Descriptions.Item label={T.progress} span={2}>
                <TaskProgress task={task} />
              </Descriptions.Item>
              <Descriptions.Item label={T.createdAt}>{formatTime(task.created_at)}</Descriptions.Item>
              <Descriptions.Item label={T.startedAt}>{formatTime(task.started_at)}</Descriptions.Item>
              <Descriptions.Item label={T.finishedAt}>
                {formatTime(task.finished_at)}
              </Descriptions.Item>
              <Descriptions.Item label={TASK_TEXT.column.attempt}>
                {task.attempt}/{task.max_attempts}
              </Descriptions.Item>
            </Descriptions>

            <Divider orientation="left">{T.params}</Divider>
            <pre style={preStyle}>{formatJson(task.params)}</pre>

            {task.error && (
              <>
                <Divider orientation="left">{T.error}</Divider>
                <Alert type="error" message={task.error} />
              </>
            )}

            {task.result && (
              <>
                <Divider orientation="left">{T.result}</Divider>
                <SyncResultView result={task.result} />
              </>
            )}

            <Divider orientation="left">{T.logs}</Divider>
            <div ref={logBox} style={{ ...preStyle, maxHeight: 360, whiteSpace: "normal" }}>
              {logs.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={T.noLogs} />
              ) : (
                groupByAttempt(logs).map(([attempt, items]) => (
                  <div key={attempt} style={{ marginBottom: 12 }}>
                    <Typography.Text strong>{T.attempt(attempt)}</Typography.Text>
                    {items.map((log) => (
                      <div
                        key={log.id}
                        style={{ fontFamily: "monospace", whiteSpace: "pre-wrap", marginTop: 2 }}
                      >
                        <Typography.Text type="secondary">{formatTime(log.ts)}</Typography.Text>{" "}
                        <Tag color={LEVEL_COLOR[log.level]} style={{ marginInlineEnd: 4 }}>
                          {TASK_TEXT.level[log.level]}
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
      </>
    );
  }

  return (
    <Card
      title={task ? `${T.title} · ${T.label(task.id)}` : T.title}
      extra={
        task && (
          <TaskActions
            task={task}
            onDone={(updated, text) => {
              message.success(text);
              onChanged(updated);
            }}
            onError={(text) => message.error(text)}
            size="middle"
          />
        )
      }
    >
      {body}
    </Card>
  );
}
