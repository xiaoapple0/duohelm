import { Alert, Checkbox, Form, Input, Modal, Typography } from "antd";
import { useState } from "react";

import { api, type EndpointScope, type PluginEndpoint } from "../api/client";

export const SCOPE_TEXT: Record<EndpointScope, { label: string; rule: string }> = {
  loopback: { label: "仅本机", rule: "只接受 127.0.0.1 或 [::1]，不接受主机名" },
  lan: {
    label: "局域网",
    rule: "只接受 IP 地址：本机、10.x、172.16–31.x、192.168.x 或 fc00::/7，不接受主机名",
  },
  internet: { label: "公网", rule: "可以填 IP 地址或主机名；保存前需要额外确认" },
};

interface Props {
  pluginId: string;
  pluginName: string;
  endpoint: PluginEndpoint | null;
  onClose: () => void;
  onSaved: (endpoint: PluginEndpoint) => void;
}

interface FormValues {
  address: string;
  confirm: boolean;
  confirmInternet: boolean;
}

export default function EndpointModal({ pluginId, pluginName, endpoint, onClose, onSaved }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string>();
  const address = Form.useWatch("address", form) ?? "";
  const internet = endpoint?.scope === "internet";

  async function submit() {
    if (!endpoint) {
      return;
    }
    const values = await form.validateFields();
    setSubmitting(true);
    setError(undefined);
    try {
      onSaved(
        await api.confirmEndpoint(pluginId, endpoint.id, {
          address: values.address.trim(),
          confirm: true,
          confirm_internet: internet ? values.confirmInternet : false,
          digest: endpoint.digest,
        }),
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const mustCheck = (message: string) => ({
    validator: (_: unknown, value: boolean) =>
      value ? Promise.resolve() : Promise.reject(new Error(message)),
  });

  return (
    <Modal
      open={endpoint !== null}
      title={endpoint ? `设置连接地址：${endpoint.description}` : ""}
      okText="确认并保存"
      confirmLoading={submitting}
      onOk={() => void submit()}
      onCancel={() => {
        setError(undefined);
        onClose();
      }}
      destroyOnHidden
    >
      {endpoint && (
        <>
          <Typography.Paragraph type="secondary">
            范围：{SCOPE_TEXT[endpoint.scope].label}。{SCOPE_TEXT[endpoint.scope].rule}。默认地址{" "}
            <Typography.Text code>{endpoint.default}</Typography.Text>
          </Typography.Paragraph>
          {endpoint.state === "needs_confirmation" && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="插件清单里的连接设置变了，之前保存的地址需要重新确认才会使用"
            />
          )}
          <Form<FormValues>
            form={form}
            layout="vertical"
            preserve={false}
            initialValues={{
              address: endpoint.configured ?? endpoint.default,
              confirm: false,
              confirmInternet: false,
            }}
          >
            <Form.Item
              name="address"
              label="地址（主机:端口，IPv6 写成 [地址]:端口）"
              rules={[{ required: true, whitespace: true, message: "请填写地址" }]}
            >
              <Input autoComplete="off" spellCheck={false} />
            </Form.Item>
            <Form.Item
              name="confirm"
              valuePropName="checked"
              rules={[mustCheck("请确认")]}
            >
              <Checkbox>
                我确认：插件 {pluginName} 将可以连接{" "}
                <Typography.Text code>{address.trim() || "（未填写）"}</Typography.Text>
              </Checkbox>
            </Form.Item>
            {internet && (
              <Form.Item
                name="confirmInternet"
                valuePropName="checked"
                rules={[mustCheck("公网地址需要额外确认")]}
              >
                <Checkbox>我知道这个地址可能在公网上，插件的数据会经过外部网络</Checkbox>
              </Form.Item>
            )}
          </Form>
          {error && <Alert type="error" showIcon message={error} />}
        </>
      )}
    </Modal>
  );
}
