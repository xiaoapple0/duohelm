import { Alert, Descriptions, Divider, Drawer, Spin } from "antd";

import { isUnfinished, type Task } from "../../api/client";
import { formatJson, formatTime } from "./format";
import { RetryHint, StatusTag, TaskActions, TaskProgress } from "./TaskBits";
import TaskLogs, { preStyle } from "./TaskLogs";
import { TEXT } from "./text";
import { useTaskFeed } from "./useTaskFeed";

interface Props {
  taskId: number | undefined;
  onClose: () => void;
  onChanged: (task: Task, message: string) => void;
  onError: (message: string) => void;
}

export default function TaskDrawer({ taskId, onClose, onChanged, onError }: Props) {
  const { task, logs, error, refresh, setTask } = useTaskFeed(taskId);

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
          <TaskLogs logs={logs} following={isUnfinished(task.status)} />
        </>
      )}
    </Drawer>
  );
}
