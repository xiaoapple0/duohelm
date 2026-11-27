import { ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Descriptions, Input, Select, Space, Switch, Typography } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

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
  /** 取最近任务失败的原因；有值时“上次定时任务”显示为未知 */
  lastTaskError: string | undefined;
  plugins: Plugin[] | undefined;
  onSaved: (schedule: DataSyncSchedule) => void;
  onSelectTask: (id: number) => void;
}

function ScheduleForm({ schedule, lastTask, lastTaskError, plugins, onSaved, onSelectTask }: LoadedProps) {
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
          {lastTaskError ? (
            <Typography.Text type="warning">{T.lastRunUnknown(lastTaskError)}</Typography.Text>
          ) : lastTask ? (
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
  // 规则和“上次定时任务”分开取：任务列表取失败不影响规则的查看和修改。
  // 任务结束（refreshKey 变）只重取最近任务，不重取规则，避免覆盖表单里没保存的修改；
  // 规则只在首次加载、保存成功、点“刷新”或“重试”成功时以服务端为准。
  // 规则自己管理状态：重新加载成功才换成新值；失败时规则是“未知”，不显示旧值的可编辑表单。
  const [schedule, setSchedule] = useState<DataSyncSchedule>();
  const [scheduleError, setScheduleError] = useState<string>();
  const [scheduleLoading, setScheduleLoading] = useState(true);
  const seq = useRef(0);

  const loadSchedule = useCallback(async () => {
    const mine = ++seq.current;
    setScheduleLoading(true);
    try {
      const fresh = await api.dataSyncSchedule();
      if (mine === seq.current) {
        setSchedule(fresh);
        setScheduleError(undefined);
      }
    } catch (err: unknown) {
      if (mine === seq.current) {
        setSchedule(undefined);
        setScheduleError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      if (mine === seq.current) {
        setScheduleLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void loadSchedule();
  }, [loadSchedule]);

  const loadLast = useCallback(() => {
    void refreshKey;
    return loadLastScheduledTask();
  }, [refreshKey]);
  const lastRequest = useRequest(loadLast);

  const loading = scheduleLoading || lastRequest.loading;

  function refresh() {
    void loadSchedule();
    lastRequest.reload();
  }

  return (
    <Card
      title={T.title}
      loading={scheduleLoading && !schedule && !scheduleError}
      extra={
        <Button icon={<ReloadOutlined />} onClick={refresh} loading={loading}>
          {T.refresh}
        </Button>
      }
    >
      {scheduleError && (
        <Alert
          type="error"
          showIcon
          message={T.loadFailed}
          description={scheduleError}
          action={
            <Button size="small" onClick={() => void loadSchedule()} loading={scheduleLoading}>
              {T.retry}
            </Button>
          }
        />
      )}
      {schedule && (
        <ScheduleForm
          schedule={schedule}
          lastTask={lastRequest.error ? undefined : lastRequest.data}
          lastTaskError={lastRequest.error?.message}
          plugins={plugins}
          onSaved={setSchedule}
          onSelectTask={onSelectTask}
        />
      )}
    </Card>
  );
}
