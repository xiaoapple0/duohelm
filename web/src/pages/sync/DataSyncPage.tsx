import { Alert, Space, Typography } from "antd";
import { useEffect, useState } from "react";

import { api, isUnfinished, type Task } from "../../api/client";
import { useRequest } from "../../api/useRequest";
import { useTaskFeed } from "../tasks/useTaskFeed";
import ManualSync from "./ManualSync";
import SchedulePanel from "./SchedulePanel";
import { SYNC_TASK_TYPE } from "./keys";
import TaskPanel from "./TaskPanel";
import { TEXT } from "./text";

export default function DataSyncPage() {
  const plugins = useRequest(api.plugins);
  const [taskId, setTaskId] = useState<number>();
  const [loadingLatest, setLoadingLatest] = useState(true);
  const [scheduleKey, setScheduleKey] = useState(0);
  const feed = useTaskFeed(taskId);

  // 打开页面时接上最近一个同步任务（手动或定时提交的都算），刷新页面不会丢
  useEffect(() => {
    let cancelled = false;
    api
      .tasks({ type: SYNC_TASK_TYPE, limit: 1 })
      .then((tasks) => {
        if (!cancelled && tasks[0]) {
          setTaskId(tasks[0].id);
        }
      })
      .catch(() => {
        // 取不到最近的任务时页面照常使用，显示“还没有同步任务”
      })
      .finally(() => {
        if (!cancelled) {
          setLoadingLatest(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const busy = feed.task !== undefined && isUnfinished(feed.task.status);
  const finished = feed.task !== undefined && !busy;

  // 任务结束后刷新定时面板里“上次定时任务”
  useEffect(() => {
    if (finished) {
      setScheduleKey((k) => k + 1);
    }
  }, [finished, feed.task?.id]);

  const onCreated = (task: Task) => {
    setTaskId(task.id);
    feed.setTask(task);
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Typography.Title level={3} style={{ margin: 0 }}>
        {TEXT.title}
      </Typography.Title>
      {plugins.error && (
        <Alert type="error" showIcon message={TEXT.loadFailed} description={plugins.error.message} />
      )}
      <ManualSync plugins={plugins.data} busy={busy} onCreated={onCreated} />
      <TaskPanel
        taskId={taskId}
        task={feed.task}
        logs={feed.logs}
        error={feed.error}
        loading={loadingLatest}
        onChanged={(task) => {
          feed.setTask(task);
          void feed.refresh();
        }}
      />
      <SchedulePanel plugins={plugins.data} refreshKey={scheduleKey} onSelectTask={setTaskId} />
    </Space>
  );
}
