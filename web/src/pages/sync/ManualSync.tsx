import { Alert, App, Button, Card, Form, Input, Select, Space, Typography } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, type Plugin, type Task } from "../../api/client";
import { isValidDate, lastClosedDay } from "./date";
import { EARLIEST_START, MANUAL_DEDUPE_KEY, SYNC_TASK_TYPE } from "./keys";
import { TEXT } from "./text";

const T = TEXT.manual;

/** 能选的行情源：kind 为 datasource 的插件。 */
export function datasources(plugins: Plugin[] | undefined): Plugin[] {
  return (plugins ?? []).filter((p) => p.kind === "datasource");
}

/** 插件上还没确认的连接地址和联网权限，确认前插件连不上。 */
function unconfirmed(plugin: Plugin | undefined): string[] {
  if (!plugin) {
    return [];
  }
  const items = plugin.endpoints
    .filter((e) => e.state !== "confirmed")
    .map((e) => T.endpoint(e.id, T.endpointState[e.state === "default" ? "default" : "needs_confirmation"]));
  if (plugin.network?.state === "needs_approval") {
    items.push(T.network);
  }
  return items;
}

interface Props {
  plugins: Plugin[] | undefined;
  /** 已经有同步任务在排队或运行 */
  busy: boolean;
  onCreated: (task: Task) => void;
}

export default function ManualSync({ plugins, busy, onCreated }: Props) {
  const { message } = App.useApp();
  const sources = datasources(plugins);
  const [pluginId, setPluginId] = useState<string>();
  const [start, setStart] = useState(EARLIEST_START);
  const [end, setEnd] = useState(() => lastClosedDay());
  const [submitting, setSubmitting] = useState(false);

  // 默认选第一个可用的行情源
  useEffect(() => {
    if (pluginId === undefined) {
      const first = sources.find((p) => p.status === "ready");
      if (first) {
        setPluginId(first.id);
      }
    }
  }, [sources, pluginId]);

  const selected = sources.find((p) => p.id === pluginId);
  const missing = unconfirmed(selected);

  let startError: string | undefined;
  let endError: string | undefined;
  if (!isValidDate(start)) {
    startError = T.startInvalid;
  }
  if (!isValidDate(end)) {
    endError = T.endInvalid;
  } else {
    const closed = lastClosedDay();
    if (end > closed) {
      endError = T.endNotClosed(closed);
    } else if (!startError && end < start) {
      endError = T.endBeforeStart;
    }
  }

  const canSubmit =
    selected !== undefined &&
    selected.status === "ready" &&
    missing.length === 0 &&
    !startError &&
    !endError &&
    !busy;

  async function submit() {
    if (!selected || !canSubmit) {
      return;
    }
    setSubmitting(true);
    try {
      const task = await api.createTask({
        type: SYNC_TASK_TYPE,
        params: { plugin_id: selected.id, start, end },
        dedupe_key: MANUAL_DEDUPE_KEY,
      });
      message.success(T.created(task.id));
      onCreated(task);
    } catch (err: unknown) {
      const reason = err instanceof Error ? err.message : String(err);
      message.error(T.createFailed(err instanceof ApiError && err.status === 409 ? T.busy : reason));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title={T.title}>
      <Form layout="vertical" style={{ maxWidth: 480 }}>
        <Form.Item
          label={T.plugin}
          required
          validateStatus={sources.length === 0 && plugins ? "warning" : undefined}
          help={sources.length === 0 && plugins ? T.noPlugins : undefined}
        >
          <Select
            value={pluginId}
            onChange={setPluginId}
            placeholder={T.pluginPlaceholder}
            loading={plugins === undefined}
            options={sources.map((p) => ({
              value: p.id,
              disabled: p.status !== "ready",
              label: `${p.name ?? p.id}（${p.id}${p.version ? ` ${p.version}` : ""}）${p.status === "ready" ? "" : T.notReady(p.status)}`,
            }))}
          />
        </Form.Item>

        {missing.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 24 }}
            message={T.needConfirm}
            description={
              <Space direction="vertical" size={4}>
                <ul style={{ margin: 0, paddingInlineStart: 20 }}>
                  {missing.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
                <Link to="/plugins">{T.goPlugins}</Link>
              </Space>
            }
          />
        )}

        <Form.Item label={T.start} required validateStatus={startError ? "error" : undefined} help={startError}>
          <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </Form.Item>
        <Form.Item
          label={T.end}
          required
          validateStatus={endError ? "error" : undefined}
          help={endError ?? <Typography.Text type="secondary">{T.endHint}</Typography.Text>}
        >
          <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </Form.Item>

        <Space direction="vertical" size={4}>
          <Button type="primary" disabled={!canSubmit} loading={submitting} onClick={() => void submit()}>
            {T.submit}
          </Button>
          {busy && <Typography.Text type="secondary">{T.busy}</Typography.Text>}
        </Space>
      </Form>
    </Card>
  );
}
