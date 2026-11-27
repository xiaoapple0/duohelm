import { Alert, Button, Space, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, isUnfinished, type Task } from "../../api/client";
import { usePolling } from "../../api/usePolling";
import { useRequest } from "../../api/useRequest";
import { useTaskFeed } from "../tasks/useTaskFeed";
import { SYNC_TASK_TYPE } from "./keys";
import ManualSync from "./ManualSync";
import SchedulePanel from "./SchedulePanel";
import TaskPanel from "./TaskPanel";
import { TEXT } from "./text";

const ACTIVE_POLL_MS = 3000;
const RECENT_LIMIT = 50;

export default function DataSyncPage() {
  const plugins = useRequest(api.plugins);
  // 正在查看的任务；和“有没有未结束的同步任务”是两回事
  const [taskId, setTaskId] = useState<number>();
  const [loadingLatest, setLoadingLatest] = useState(true);
  // 最近一个未结束的 data.sync 任务（不论手动还是定时提交的），决定能不能开始新的同步
  const [active, setActive] = useState<Task>();
  const activeRef = useRef<Task | undefined>(undefined);
  const [scheduleKey, setScheduleKey] = useState(0);
  const feed = useTaskFeed(taskId);

  const refreshActive = useCallback(async (): Promise<Task[] | undefined> => {
    try {
      const tasks = await api.tasks({ type: SYNC_TASK_TYPE, limit: RECENT_LIMIT });
      const running = tasks.find((t) => isUnfinished(t.status));
      const previous = activeRef.current;
      activeRef.current = running;
      setActive(running);
      // 只在任务从“未结束”变为“已结束”时，让定时面板重取“上次定时任务”
      if (previous && previous.id !== running?.id) {
        setScheduleKey((k) => k + 1);
      }
      return tasks;
    } catch {
      return undefined; // 取不到时保持原状，下一轮再试
    }
  }, []);

  // 打开页面时：有未结束的任务就接上它，否则显示最近一个（手动或定时提交的都算）
  useEffect(() => {
    let cancelled = false;
    void refreshActive().then((tasks) => {
      if (cancelled) {
        return;
      }
      const first = activeRef.current ?? tasks?.[0];
      if (first) {
        setTaskId(first.id);
      }
      setLoadingLatest(false);
    });
    return () => {
      cancelled = true;
    };
  }, [refreshActive]);

  usePolling(
    async () => {
      await refreshActive();
    },
    ACTIVE_POLL_MS,
    true,
  );

  // 正在查看的任务状态变了（结束、取消），马上重新判断有没有未结束的任务
  const viewedStatus = feed.task?.status;
  useEffect(() => {
    if (viewedStatus !== undefined) {
      void refreshActive();
    }
  }, [viewedStatus, refreshActive]);

  const onCreated = (task: Task) => {
    activeRef.current = task;
    setActive(task);
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
      <ManualSync plugins={plugins.data} busy={active !== undefined} onCreated={onCreated} />
      {active && active.id !== taskId && (
        <Alert
          type="info"
          showIcon
          message={TEXT.page.viewingOther(active.id)}
          action={
            <Button size="small" onClick={() => setTaskId(active.id)}>
              {TEXT.page.backToActive}
            </Button>
          }
        />
      )}
      <TaskPanel
        taskId={taskId}
        task={feed.task}
        logs={feed.logs}
        error={feed.error}
        loading={loadingLatest}
        onChanged={(task) => {
          feed.setTask(task);
          void feed.refresh();
          void refreshActive();
        }}
      />
      <SchedulePanel plugins={plugins.data} refreshKey={scheduleKey} onSelectTask={setTaskId} />
    </Space>
  );
}
