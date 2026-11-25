import { Alert, Card, Descriptions, Spin } from "antd";

import { api } from "../api/client";
import { useRequest } from "../api/useRequest";

export default function OverviewPage() {
  const { data, error, loading } = useRequest(api.health);

  return (
    <Card title="概览">
      {loading && <Spin />}
      {error && (
        <Alert
          type="error"
          showIcon
          message="无法连接后端"
          description={`${error.message}。请确认已运行 duohelm serve。`}
        />
      )}
      {data && (
        <Descriptions column={1} bordered size="small">
          <Descriptions.Item label="后端状态">{data.status}</Descriptions.Item>
          <Descriptions.Item label="平台版本">{data.version}</Descriptions.Item>
          <Descriptions.Item label="SDK 版本">{data.sdk_version}</Descriptions.Item>
        </Descriptions>
      )}
    </Card>
  );
}
