import { Alert, App, Card, Descriptions, Divider, Empty, Spin } from "antd";

import { isUnfinished, type Task, type TaskLog } from "../../api/client";
import { formatJson, formatTime } from "../tasks/format";
import { RetryHint, StatusTag, TaskActions, TaskProgress } from "../tasks/TaskBits";
import TaskLogs, { preStyle } from "../tasks/TaskLogs";
import { TEXT as TASK_TEXT } from "../tasks/text";
import { MANUAL_DEDUPE_KEY, SCHEDULE_DEDUPE_KEY } from "./keys";
import SyncResultView from "./SyncResultView";
import { TEXT } from "./text";

const T = TEXT.task;

function source(task: Task): string {
  if (task.dedupe_key === MANUAL_DEDUPE_KEY) {
    return T.manual;
  }
  if (task.dedupe_key === SCHEDULE_DEDUPE_KEY) {
    return T.scheduled;
  }
  return T.other;
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
            <TaskLogs logs={logs} following={isUnfinished(task.status)} />
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
