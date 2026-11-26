import { ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { useState } from "react";

import {
  api,
  type EndpointState,
  type NetworkState,
  type Plugin,
  type PluginEndpoint,
  type PluginKind,
  type PluginNetwork,
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

const ENDPOINT_STATE: Record<EndpointState, { color: string; label: string }> =
  {
    default: { color: "orange", label: "未确认，不能连接" },
    confirmed: { color: "green", label: "已确认" },
    needs_confirmation: { color: "orange", label: "需要重新确认，不能连接" },
  };

const NETWORK_STATE: Record<NetworkState, { color: string; label: string }> = {
  none: { color: "default", label: "不联网" },
  approved: { color: "green", label: "已确认" },
  needs_approval: { color: "orange", label: "待确认，不能联网" },
};

const columns: TableColumnsType<Plugin> = [
  { title: "ID", dataIndex: "id", key: "id" },
  {
    title: "名称",
    dataIndex: "name",
    key: "name",
    render: (v: string | null) => v ?? "-",
  },
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
    render: (v: PluginStatus) => (
      <Tag color={STATUS[v].color}>{STATUS[v].label}</Tag>
    ),
  },
  {
    title: "联网",
    dataIndex: "network",
    key: "network",
    render: (network: PluginNetwork | null) => {
      if (!network || network.state === "none") {
        return "-";
      }
      const s = NETWORK_STATE[network.state];
      return (
        <Space size={4} style={{ whiteSpace: "nowrap" }}>
          <Tag color={s.color}>{s.label}</Tag>
          <span>{network.hosts.length} 个地址</span>
        </Space>
      );
    },
  },
  {
    title: "连接地址",
    dataIndex: "endpoints",
    key: "endpoints",
    render: (endpoints: PluginEndpoint[]) => {
      if (endpoints.length === 0) {
        return "-";
      }
      const pending = endpoints.filter((e) => e.state !== "confirmed").length;
      return pending > 0 ? (
        <Tag color="orange">{pending} 个未确认</Tag>
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
      void message.success("已清除，插件确认之前不能连接");
      reload();
    } catch (err: unknown) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  }

  async function confirmDefault(plugin: Plugin, endpoint: PluginEndpoint) {
    try {
      await api.confirmEndpoint(plugin.id, endpoint.id, {
        address: endpoint.default,
        confirm: true,
        digest: endpoint.digest,
      });
      void message.success("已确认，插件可以连接这个地址");
      reload();
    } catch (err: unknown) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  }

  async function approveNetwork(plugin: Plugin) {
    try {
      await api.approveNetwork(plugin.id, plugin.network?.digest ?? "");
      void message.success("已确认，插件可以连接这些地址");
      reload();
    } catch (err: unknown) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  }

  async function revokeNetwork(plugin: Plugin) {
    try {
      await api.revokeNetwork(plugin.id);
      void message.success("已撤销，插件不能再联网");
      reload();
    } catch (err: unknown) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  }

  function networkSection(plugin: Plugin) {
    const network = plugin.network;
    if (!network || network.state === "none") {
      return null;
    }
    const added = network.approved_hosts
      ? network.hosts.filter((h) => !network.approved_hosts?.includes(h))
      : [];
    const removed = network.approved_hosts
      ? network.approved_hosts.filter((h) => !network.hosts.includes(h))
      : [];
    return (
      <div style={{ marginBottom: 16 }}>
        <Space style={{ marginBottom: 8 }}>
          <Typography.Text strong>
            联网地址（{network.hosts.length} 个）
          </Typography.Text>
          <Tag color={NETWORK_STATE[network.state].color}>
            {NETWORK_STATE[network.state].label}
          </Tag>
        </Space>
        {network.state === "needs_approval" && network.approved_hosts && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 8 }}
            message="插件的联网地址列表变了，重新确认之前插件不能联网"
            description={
              <>
                {added.length > 0 && <div>新增：{added.join("、")}</div>}
                {removed.length > 0 && <div>去掉：{removed.join("、")}</div>}
              </>
            }
          />
        )}
        <ul style={{ margin: "0 0 8px", paddingLeft: 20 }}>
          {network.hosts.map((h) => (
            <li key={h}>
              <Typography.Text code style={{ whiteSpace: "nowrap" }}>
                {h}
              </Typography.Text>
            </li>
          ))}
        </ul>
        <Space>
          {network.state === "needs_approval" && (
            <Popconfirm
              title={`允许插件 ${plugin.name ?? plugin.id} 连接这 ${network.hosts.length} 个地址？`}
              description="只允许公网地址；连接时解析到本机或局域网的地址会被拦截"
              okText="确认放行"
              cancelText="取消"
              onConfirm={() => void approveNetwork(plugin)}
            >
              <Button size="small" type="primary">
                确认放行
              </Button>
            </Popconfirm>
          )}
          {network.state === "approved" && (
            <Popconfirm
              title="撤销后插件不能联网，确定？"
              okText="撤销"
              cancelText="取消"
              onConfirm={() => void revokeNetwork(plugin)}
            >
              <Button size="small">撤销确认</Button>
            </Popconfirm>
          )}
        </Space>
      </div>
    );
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
        title: "地址",
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
            {e.state === "default" && e.scope !== "internet" && (
              <Popconfirm
                title={`允许插件 ${plugin.name ?? plugin.id} 连接 ${e.default}？`}
                description="这是插件清单里的默认地址"
                okText="确认"
                cancelText="取消"
                onConfirm={() => void confirmDefault(plugin, e)}
              >
                <Button size="small" type="primary">
                  确认默认地址
                </Button>
              </Popconfirm>
            )}
            <Button
              size="small"
              type={e.state === "needs_confirmation" ? "primary" : "default"}
              onClick={() => setEditing({ plugin, endpoint: e })}
            >
              {e.state === "needs_confirmation"
                ? "重新确认"
                : e.state === "default" && e.scope === "internet"
                  ? "确认地址"
                  : "修改地址"}
            </Button>
            {e.configured !== null && (
              <Popconfirm
                title="清除已保存的地址？"
                description="清除后回到未确认：插件拿不到地址，也不能连接，直到重新确认"
                okText="清除"
                cancelText="取消"
                onConfirm={() => void clear(plugin, e)}
              >
                <Button size="small">清除</Button>
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
      {error && (
        <Alert
          type="error"
          showIcon
          message={error.message}
          style={{ marginBottom: 16 }}
        />
      )}
      <Table<Plugin>
        rowKey={(p) => p.path}
        columns={columns}
        dataSource={data ?? []}
        loading={loading}
        pagination={false}
        expandable={{
          expandedRowRender: (p) => (
            <>
              {networkSection(p)}
              {p.endpoints.length > 0 && endpointTable(p)}
            </>
          ),
          rowExpandable: (p) =>
            p.endpoints.length > 0 ||
            (p.network !== null && p.network.state !== "none"),
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
