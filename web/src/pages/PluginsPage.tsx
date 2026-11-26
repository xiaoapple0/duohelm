import { ReloadOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Popconfirm, Space, Table, Tag, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { useState } from "react";

import {
  api,
  type EndpointState,
  type Plugin,
  type PluginEndpoint,
  type PluginKind,
  type PluginStatus,
} from "../api/client";
import { useRequest } from "../api/useRequest";
import EndpointModal, { SCOPE_TEXT } from "./EndpointModal";

const STATUS: Record<PluginStatus, { color: string; label: string }> = {
  ready: { color: "green", label: "可用" },
  invalid: { color: "red", label: "清单无效" },
  incompatible: { color: "orange", label: "SDK 不兼容" },
  blocked: { color: "gold", label: "依赖不满足" },
};

const KIND: Record<PluginKind, string> = {
  datasource: "行情源",
  operator: "算子",
  factor: "因子",
  broker: "交易接口",
};

const ENDPOINT_STATE: Record<EndpointState, { color: string; label: string }> = {
  default: { color: "default", label: "默认地址" },
  confirmed: { color: "green", label: "已确认" },
  needs_confirmation: { color: "orange", label: "需要重新确认" },
};

const columns: TableColumnsType<Plugin> = [
  { title: "ID", dataIndex: "id", key: "id" },
  { title: "名称", dataIndex: "name", key: "name", render: (v: string | null) => v ?? "-" },
  {
    title: "类型",
    dataIndex: "kind",
    key: "kind",
    render: (v: PluginKind | null) => (v ? KIND[v] : "-"),
  },
  {
    title: "版本",
    dataIndex: "version",
    key: "version",
    render: (v: string | null) => v ?? "-",
  },
  {
    title: "状态",
    dataIndex: "status",
    key: "status",
    render: (v: PluginStatus) => <Tag color={STATUS[v].color}>{STATUS[v].label}</Tag>,
  },
  {
    title: "连接地址",
    dataIndex: "endpoints",
    key: "endpoints",
    render: (endpoints: PluginEndpoint[]) => {
      if (endpoints.length === 0) {
        return "-";
      }
      const pending = endpoints.filter((e) => e.state === "needs_confirmation").length;
      return pending > 0 ? (
        <Tag color="orange">{pending} 个需要重新确认</Tag>
      ) : (
        `${endpoints.length} 个`
      );
    },
  },
  {
    title: "问题",
    dataIndex: "problems",
    key: "problems",
    render: (problems: string[]) =>
      problems.length === 0 ? (
        "-"
      ) : (
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {problems.map((p) => (
            <li key={p}>
              <Typography.Text type="danger">{p}</Typography.Text>
            </li>
          ))}
        </ul>
      ),
  },
];

interface Editing {
  plugin: Plugin;
  endpoint: PluginEndpoint;
}

export default function PluginsPage() {
  const { data, error, loading, reload } = useRequest(api.plugins);
  const { message } = App.useApp();
  const [editing, setEditing] = useState<Editing | null>(null);

  async function clear(plugin: Plugin, endpoint: PluginEndpoint) {
    try {
      await api.clearEndpoint(plugin.id, endpoint.id);
      void message.success("已恢复为默认地址");
      reload();
    } catch (err: unknown) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  }

  function endpointTable(plugin: Plugin) {
    const endpointColumns: TableColumnsType<PluginEndpoint> = [
      { title: "服务", dataIndex: "description", key: "description" },
      {
        title: "范围",
        dataIndex: "scope",
        key: "scope",
        render: (v: PluginEndpoint["scope"]) => (
          <span style={{ whiteSpace: "nowrap" }}>{SCOPE_TEXT[v].label}</span>
        ),
      },
      {
        title: "当前使用",
        dataIndex: "effective",
        key: "effective",
        render: (v: string) => (
          <Typography.Text code style={{ whiteSpace: "nowrap" }}>
            {v}
          </Typography.Text>
        ),
      },
      {
        title: "状态",
        dataIndex: "state",
        key: "state",
        render: (v: EndpointState, e: PluginEndpoint) => (
          <Space direction="vertical" size={0}>
            <Tag color={ENDPOINT_STATE[v].color}>{ENDPOINT_STATE[v].label}</Tag>
            {v === "needs_confirmation" && e.configured && (
              <Typography.Text type="secondary">
                已保存{" "}
                <Typography.Text code style={{ whiteSpace: "nowrap" }}>
                  {e.configured}
                </Typography.Text>
                ，确认后才会使用
              </Typography.Text>
            )}
          </Space>
        ),
      },
      {
        title: "操作",
        key: "actions",
        render: (_: unknown, e: PluginEndpoint) => (
          <Space>
            <Button size="small" onClick={() => setEditing({ plugin, endpoint: e })}>
              {e.state === "needs_confirmation" ? "重新确认" : "设置"}
            </Button>
            {e.configured !== null && (
              <Popconfirm
                title="恢复为默认地址？"
                description={`插件将改为连接 ${e.default}`}
                okText="恢复"
                cancelText="取消"
                onConfirm={() => void clear(plugin, e)}
              >
                <Button size="small">恢复默认</Button>
              </Popconfirm>
            )}
          </Space>
        ),
      },
    ];
    return (
      <Table<PluginEndpoint>
        rowKey="id"
        size="small"
        columns={endpointColumns}
        dataSource={plugin.endpoints}
        pagination={false}
      />
    );
  }

  return (
    <Card
      title="插件"
      extra={
        <Button icon={<ReloadOutlined />} onClick={reload}>
          重新扫描
        </Button>
      }
    >
      {error && <Alert type="error" showIcon message={error.message} style={{ marginBottom: 16 }} />}
      <Table<Plugin>
        rowKey={(p) => p.path}
        columns={columns}
        dataSource={data ?? []}
        loading={loading}
        pagination={false}
        expandable={{
          expandedRowRender: endpointTable,
          rowExpandable: (p) => p.endpoints.length > 0,
        }}
        locale={{ emptyText: "插件目录里还没有插件" }}
      />
      <EndpointModal
        pluginId={editing?.plugin.id ?? ""}
        pluginName={editing?.plugin.name ?? editing?.plugin.id ?? ""}
        endpoint={editing?.endpoint ?? null}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          void message.success("连接地址已确认");
          reload();
        }}
      />
    </Card>
  );
}
