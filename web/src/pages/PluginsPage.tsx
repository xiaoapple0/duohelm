import { ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Table, Tag, Typography } from "antd";
import type { TableColumnsType } from "antd";

import { api, type Plugin, type PluginKind, type PluginStatus } from "../api/client";
import { useRequest } from "../api/useRequest";

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

export default function PluginsPage() {
  const { data, error, loading, reload } = useRequest(api.plugins);

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
        locale={{ emptyText: "插件目录里还没有插件" }}
      />
    </Card>
  );
}
