import { Alert, Form, Input, Modal, Select, Typography } from "antd";
import { useState } from "react";

import { api, type JsonObject, type Task, type TaskTypeInfo } from "../../api/client";
import { formatJson } from "./format";
import { TEXT } from "./text";

interface Props {
  open: boolean;
  types: TaskTypeInfo[];
  onClose: () => void;
  onCreated: (task: Task) => void;
}

interface FormValues {
  type: string;
  params: string;
}

/** 从参数的 JSON Schema 里取出各字段的默认值，作为输入框的初始内容。 */
function defaultParams(info: TaskTypeInfo | undefined): string {
  const properties = info?.params_schema.properties;
  if (typeof properties !== "object" || properties === null) {
    return "{}";
  }
  const defaults: JsonObject = {};
  for (const [key, schema] of Object.entries(properties as Record<string, unknown>)) {
    if (typeof schema === "object" && schema !== null && "default" in schema) {
      defaults[key] = (schema as { default: unknown }).default;
    }
  }
  return formatJson(defaults);
}

function parseParams(text: string): JsonObject | undefined {
  if (!text.trim()) {
    return {};
  }
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      return value as JsonObject;
    }
  } catch {
    // 落到下面的 undefined
  }
  return undefined;
}

export default function CreateTaskModal({ open, types, onClose, onCreated }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string>();
  const selected = Form.useWatch("type", form);
  const info = types.find((t) => t.name === selected);

  async function submit() {
    const values = await form.validateFields();
    const params = parseParams(values.params);
    if (!params) {
      setError(TEXT.createModal.paramsInvalid);
      return;
    }
    setSubmitting(true);
    setError(undefined);
    try {
      onCreated(await api.createTask({ type: values.type, params }));
      form.resetFields();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open={open}
      title={TEXT.createModal.title}
      okText={TEXT.createModal.submit}
      confirmLoading={submitting}
      onOk={() => void submit()}
      onCancel={() => {
        setError(undefined);
        onClose();
      }}
      destroyOnHidden
    >
      {types.length === 0 && <Alert type="info" message={TEXT.createModal.noTypes} />}
      <Form<FormValues>
        form={form}
        layout="vertical"
        preserve={false}
        onValuesChange={(changed: Partial<FormValues>) => {
          if (changed.type !== undefined) {
            const next = types.find((t) => t.name === changed.type);
            form.setFieldValue("params", defaultParams(next));
            setError(undefined);
          }
        }}
      >
        <Form.Item
          name="type"
          label={TEXT.createModal.type}
          rules={[{ required: true, message: TEXT.createModal.typeRequired }]}
        >
          <Select
            options={types.map((t) => ({ value: t.name, label: t.name }))}
            placeholder={TEXT.createModal.typeRequired}
          />
        </Form.Item>
        {info?.description && (
          <Typography.Paragraph type="secondary">{info.description}</Typography.Paragraph>
        )}
        <Form.Item
          name="params"
          label={TEXT.createModal.params}
          extra={TEXT.createModal.paramsHint}
          initialValue="{}"
        >
          <Input.TextArea rows={6} style={{ fontFamily: "monospace" }} />
        </Form.Item>
      </Form>
      {error && <Alert type="error" showIcon message={error} />}
    </Modal>
  );
}
