import { Card, Empty, Tag, Typography } from "antd";

interface Props {
  title: string;
  milestone?: string;
  description?: string;
}

export default function PlaceholderPage({ title, milestone, description }: Props) {
  return (
    <Card
      title={title}
      extra={milestone ? <Tag color="blue">计划于 {milestone}</Tag> : undefined}
    >
      <Empty
        description={<Typography.Text type="secondary">{description ?? "尚未实现"}</Typography.Text>}
      />
    </Card>
  );
}
