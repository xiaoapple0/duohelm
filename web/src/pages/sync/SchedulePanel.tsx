import { ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Descriptions, Input, Select, Space, Switch, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";

import {
  api,
  type DataSyncSchedule,
  type DataSyncSchedulePatch,
  type Plugin,
  type Task,
} from "../../api/client";
import { useRequest } from "../../api/useRequest";
import { StatusTag } from "../tasks/TaskBits";
import { formatTime } from "../tasks/format";
import { datasources } from "./ManualSync";
import { formatBeijing, isAfterClose } from "./date";
import { SCHEDULE_DEDUPE_KEY, SYNC_TASK_TYPE } from "./keys";
import { TEXT } from "./text";

const T = TEXT.schedule;

interface LoadedProps {
  schedule: DataSyncSchedule;
  lastTask: Task | undefined;
  plugins: Plugin[] | undefined;
  onSaved: (schedule: DataSyncSchedule) => void;
  onSelectTask: (id: number) => void;
}

function ScheduleForm({ schedule, lastTask, plugins, onSaved, onSelectTask }: LoadedProps) {
  const { message } = App.useApp();
  const [enabled, setEnabled] = useState(schedule.enabled);
  const [pluginId, setPluginId] = useState(schedule.plugin_id ?? undefined);
  const [time, setTime] = useState(schedule.time);
  const [saving, setSaving] = useState(false);

  // 服务端的值变了（保存后、刷新后）就以它为准
  useEffect(() => {
    setEnabled(schedule.enabled);
    setPluginId(schedule.plugin_id ?? undefined);
    setTime(schedule.time);
  }, [schedule]);

  const sources = datasources(plugins);
  const timeError = isAfterClose(time) === true ? undefined : T.timeInvalid;
  const pluginError = enabled && !pluginId ? T.needPlugin : undefined;

  // 只提交改动过的字段：平台默认值和没改的字段不覆盖用户已设的值
  const patch: DataSyncSchedulePatch = {};
  if (enabled !== schedule.enabled) {
    patch.enabled = enabled;
  }
  if (pluginId !== undefined && pluginId !== schedule.plugin_id) {
    patch.plugin_id = pluginId;
  }
  if (time !== schedule.time) {
    patch.time = time;
  }
  const changed = Object.keys(patch).length > 0;

  async function save() {
    setSaving(true);
    try {
      const saved = await api.updateDataSyncSchedule(patch);
      message.success(T.saved);
      onSaved(saved);
    } catch (err: unknown) {
      message.error(T.saveFailed(err instanceof Error ? err.message : String(err)));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Text type="secondary">{T.desc}</Typography.Text>

      {schedule.needs_full_sync === true && <Alert type="warning" showIcon message={T.needsFullSync} />}
      {schedule.state_error && (
        <Alert type="error" showIcon message={T.stateError(schedule.state_error)} />
      )}
      {schedule.plugin_warning && <Alert type="warning" showIcon message={schedule.plugin_warning} />}

      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label={T.nextRun}>
          <span title={T.nextRunHint}>{formatBeijing(schedule.next_run_at)}</span>
        </Descriptions.Item>
        <Descriptions.Item label={T.lastTrigger}>
          <span title={T.lastTriggerHint}>{schedule.last_trigger_date ?? T.lastTriggerNone}</span>
        </Descriptions.Item>
        <Descriptions.Item label={T.lastRun}>
          {lastTask ? (
            <Space direction="vertical" size={4}>
              <Space>
                <StatusTag task={lastTask} />
                <Typography.Text type="secondary">{formatTime(lastTask.created_at)}</Typography.Text>
                <Button type="link" size="small" style={{ padding: 0 }} onClick={() => onSelectTask(lastTask.id)}>
                  {`#${lastTask.id}`}
                </Button>
              </Space>
              {lastTask.error && <Typography.Text type="danger">{lastTask.error}</Typography.Text>}
            </Space>
          ) : (
            <Typography.Text type="secondary" title={T.lastRunHint}>
              {T.lastRunNone}
            </Typography.Text>
          )}
        </Descriptions.Item>
      </Descriptions>

      <Space direction="vertical" size="middle" style={{ width: "100%", maxWidth: 480 }}>
        <Space>
          <Switch checked={enabled} onChange={setEnabled} />
          <span>{T.enabled}</span>
        </Space>
        <div>
          <div>{T.plugin}</div>
          <Select
            style={{ width: "100%" }}
            value={pluginId}
            onChange={setPluginId}
            placeholder={T.pluginPlaceholder}
            status={pluginError ? "error" : undefined}
            loading={plugins === undefined}
            options={sources.map((p) => ({ value: p.id, label: `${p.name ?? p.id}（${p.id}）` }))}
          />
          {pluginError && <Typography.Text type="danger">{pluginError}</Typography.Text>}
        </div>
        <div>
          <div>{T.time}</div>
          <Input
            type="time"
            value={time}
            onChange={(e) => setTime(e.target.value)}
            status={timeError ? "error" : undefined}
          />
          <Typography.Text type={timeError ? "danger" : "secondary"}>
            {timeError ?? T.timeHint}
          </Typography.Text>
        </div>
        <Button
          type="primary"
          disabled={!changed || !!timeError || !!pluginError}
          loading={saving}
          onClick={() => void save()}
        >
          {T.save}
        </Button>
      </Space>
    </Space>
  );
}

interface Props {
  plugins: Plugin[] | undefined;
  /** 定时同步的任务有可能变了（例如手动同步结束后）时递增，面板重新加载 */
  refreshKey: number;
  onSelectTask: (id: number) => void;
}

/** 在最近的 data.sync 任务里找最近一个定时提交的。 */
async function loadLastScheduledTask(): Promise<Task | undefined> {
  const tasks = await api.tasks({ type: SYNC_TASK_TYPE, limit: 200 });
  return tasks.find((t) => t.dedupe_key === SCHEDULE_DEDUPE_KEY);
}

export default function SchedulePanel({ plugins, refreshKey, onSelectTask }: Props) {
  const load = useCallback(
    async () => ({
      schedule: await api.dataSyncSchedule(),
      lastTask: await loadLastScheduledTask(),
      key: refreshKey,
    }),
    [refreshKey],
  );
  const { data, error, loading, reload } = useRequest(load);
  const [saved, setSaved] = useState<DataSyncSchedule>();

  // 重新加载后以服务端为准
  useEffect(() => {
    setSaved(undefined);
  }, [data]);

  const schedule = saved ?? data?.schedule;

  return (
    <Card
      title={T.title}
      loading={loading && !data}
      extra={
        <Button icon={<ReloadOutlined />} onClick={reload} loading={loading}>
          {T.refresh}
        </Button>
      }
    >
      {error && <Alert type="error" showIcon message={TEXT.loadFailed} description={error.message} />}
      {schedule && (
        <ScheduleForm
          schedule={schedule}
          lastTask={data?.lastTask}
          plugins={plugins}
          onSaved={(s) => {
            setSaved(s);
            reload();
          }}
          onSelectTask={onSelectTask}
        />
      )}
    </Card>
  );
}
