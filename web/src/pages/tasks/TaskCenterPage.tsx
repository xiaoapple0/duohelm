import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Select, Space, Table, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, isUnfinished, type Task, type TaskStatus } from "../../api/client";
import { usePolling } from "../../api/usePolling";
import { useRequest } from "../../api/useRequest";
import CreateTaskModal from "./CreateTaskModal";
import { formatTime, taskDuration } from "./format";
import { RetryHint, StatusTag, TaskActions, TaskProgress } from "./TaskBits";
import TaskDrawer from "./TaskDrawer";
import { TEXT } from "./text";

const POLL_MS = 2000;
const PAGE_LIMIT = 100;
const STATUSES = Object.keys(TEXT.status) as TaskStatus[];

export default function TaskCenterPage() {
  const { message } = App.useApp();
  const [tasks, setTasks] = useState<Task[]>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<TaskStatus>();
  const [type, setType] = useState<string>();
  const [selectedId, setSelectedId] = useState<number>();
  const [creating, setCreating] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const types = useRequest(api.taskTypes);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const result = await api.tasks({ status, type, limit: PAGE_LIMIT });
      if (seq === requestSeq.current) {
        setTasks(result);
        setError(undefined);
        setNow(Date.now());
      }
    } catch (err: unknown) {
      if (seq === requestSeq.current) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (seq === requestSeq.current) {
        setLoading(false);
      }
    }
  }, [status, type]);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load]);

  const active = tasks?.some((t) => isUnfinished(t.status)) ?? false;
  usePolling(load, POLL_MS, active);

  const onChanged = (_task: Task, text: string) => {
    void message.success(text);
    void load();
  };
  const onError = (text: string) => void message.error(text);

  const columns: TableColumnsType<Task> = [
    { title: TEXT.column.id, dataIndex: "id", key: "id", width: 72 },
    { title: TEXT.column.type, dataIndex: "type", key: "type" },
    {
      title: TEXT.column.status,
      key: "status",
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <StatusTag task={task} />
          <RetryHint task={task} />
        </Space>
      ),
    },
    { title: TEXT.column.progress, key: "progress", render: (_, task) => <TaskProgress task={task} /> },
    {
      title: TEXT.column.attempt,
      key: "attempt",
      render: (_, task) => `${task.attempt}/${task.max_attempts}`,
    },
    {
      title: TEXT.column.createdAt,
      dataIndex: "created_at",
      key: "created_at",
      render: (value: string) => formatTime(value),
    },
    {
      title: <Tooltip title={TEXT.column.durationHint}>{TEXT.column.duration}</Tooltip>,
      key: "duration",
      render: (_, task) => taskDuration(task, now),
    },
    {
      title: TEXT.column.actions,
      key: "actions",
      render: (_, task) => (
        <Space size="small">
          <Button size="small" type="link" onClick={() => setSelectedId(task.id)}>
            {TEXT.action.detail}
          </Button>
          <TaskActions task={task} onDone={onChanged} onError={onError} />
        </Space>
      ),
    },
  ];

  return (
    <Card
      title={TEXT.title}
      extra={
        <Space>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreating(true)}
            disabled={!types.data}
          >
            {TEXT.create}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {TEXT.refresh}
          </Button>
        </Space>
      }
    >
      <Space wrap style={{ marginBottom: 16 }}>
        <Select<TaskStatus>
          allowClear
          placeholder={`${TEXT.filter.status}：${TEXT.filter.all}`}
          style={{ width: 160 }}
          value={status}
          onChange={setStatus}
          options={STATUSES.map((s) => ({ value: s, label: TEXT.status[s] }))}
        />
        <Select<string>
          allowClear
          placeholder={`${TEXT.filter.type}：${TEXT.filter.all}`}
          style={{ width: 200 }}
          value={type}
          onChange={setType}
          options={(types.data ?? []).map((t) => ({ value: t.name, label: t.name }))}
        />
        <Typography.Text type="secondary">{TEXT.showingLatest(PAGE_LIMIT)}</Typography.Text>
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message={TEXT.loadFailed}
          description={error}
          style={{ marginBottom: 16 }}
        />
      )}

      <Table<Task>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={tasks ?? []}
        loading={loading}
        pagination={false}
        locale={{ emptyText: TEXT.emptyText }}
        onRow={(task) => ({ onDoubleClick: () => setSelectedId(task.id) })}
      />

      <TaskDrawer
        taskId={selectedId}
        onClose={() => setSelectedId(undefined)}
        onChanged={onChanged}
        onError={onError}
      />

      <CreateTaskModal
        open={creating}
        types={types.data ?? []}
        onClose={() => setCreating(false)}
        onCreated={(task) => {
          setCreating(false);
          void message.success(TEXT.createModal.created(task.id));
          void load();
          setSelectedId(task.id);
        }}
      />
    </Card>
  );
}
