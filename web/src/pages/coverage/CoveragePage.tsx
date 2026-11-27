import { Alert, Button, Card, Descriptions, Select, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useState } from "react";

import { ApiError } from "../../api/client";
import { dataApi } from "../../api/data";
import type { CoverageRow } from "../../api/data";
import { useRequest } from "../../api/useRequest";
import { PAGE_SIZES, formatShanghai, loadCoverage, regroup } from "./load";
import type { Group, Loaded } from "./load";
import { TEXT } from "./text";

function SummaryTable({ title, groups }: { title: string; groups: Group[] }) {
  const data = [
    ...groups.map((g) => ({ key: String(g.label), ...g })),
    {
      key: "__total__",
      label: TEXT.summary.total,
      count: groups.reduce((n, g) => n + g.count, 0),
      noBars: groups.reduce((n, g) => n + g.noBars, 0),
      withMissing: groups.reduce((n, g) => n + g.withMissing, 0),
    },
  ];
  return (
    <Card size="small" title={title} style={{ minWidth: 260, flex: "1 1 260px" }}>
      <Table
        size="small"
        pagination={false}
        dataSource={data}
        columns={[
          {
            title: "",
            dataIndex: "label",
            render: (v: string | null) => (v === null ? <Tag>{TEXT.summary.unknownLabel}</Tag> : v),
          },
          { title: TEXT.summary.count, dataIndex: "count", align: "right" },
          { title: TEXT.summary.noBars, dataIndex: "noBars", align: "right" },
          { title: TEXT.summary.withMissing, dataIndex: "withMissing", align: "right" },
        ]}
      />
    </Card>
  );
}

function SnapshotInfo({ data }: { data: Loaded }) {
  const info = data.info;
  let created: string;
  let state: string;
  if (info.kind === "ok") {
    created = formatShanghai(info.value.created_at);
    state = info.value.is_current ? TEXT.snapshot.current : TEXT.snapshot.notCurrent;
  } else {
    created = TEXT.snapshot.createdUnknown(
      info.kind === "notInList" ? TEXT.snapshot.notInList : info.message,
    );
    state = TEXT.snapshot.stateUnknown;
  }
  return (
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label={TEXT.snapshot.id}>
        <Typography.Text code copyable>
          {data.summary.snapshot}
        </Typography.Text>
      </Descriptions.Item>
      <Descriptions.Item label={TEXT.snapshot.createdAt}>{created}</Descriptions.Item>
      <Descriptions.Item label={TEXT.snapshot.state}>{state}</Descriptions.Item>
    </Descriptions>
  );
}

function errorAlert(error: Error, title: string, notFoundTitle: string) {
  const notFound = error instanceof ApiError && error.status === 404;
  return (
    <Alert
      type={notFound ? "warning" : "error"}
      showIcon
      message={notFound ? notFoundTitle : title}
      description={notFound ? TEXT.notFound.description(error.message) : TEXT.unknown.description(error.message)}
    />
  );
}

/** 表格：服务端分页，固定读汇总所在的快照；只有种类筛选由接口支持。 */
function CoverageTable({ snapshot, asOf, kinds }: { snapshot: string; asOf: string; kinds: string[] }) {
  const [kind, setKind] = useState<string>();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZES[0] ?? 50);

  const request = useCallback(
    () => dataApi.coverage({ snapshot, kind, limit: pageSize, offset: (page - 1) * pageSize }),
    [snapshot, kind, page, pageSize],
  );
  const { data, error, loading, reload } = useRequest(request);

  const unknownTag = <Tag>{TEXT.cell.unknown}</Tag>;
  const noBars = <Typography.Text type="secondary">{TEXT.cell.noBars}</Typography.Text>;
  const columns: ColumnsType<CoverageRow> = [
    { title: TEXT.col.symbol, dataIndex: "symbol" },
    {
      title: TEXT.col.name,
      dataIndex: "name",
      render: (v: string | null) => v ?? <Typography.Text type="secondary">{TEXT.cell.noName}</Typography.Text>,
    },
    { title: TEXT.col.exchange, dataIndex: "exchange", render: (v: string | null) => v ?? unknownTag },
    { title: TEXT.col.kind, dataIndex: "kind", render: (v: string | null) => v ?? unknownTag },
    {
      title: TEXT.col.status,
      dataIndex: "delist_date",
      // exchange 为 null：证券列表里没有这个代码，退市日也是未知，不是“没有退市日”
      render: (_: string | null, r) =>
        r.exchange === null
          ? unknownTag
          : r.delist_date !== null && r.delist_date <= asOf
            ? TEXT.cell.delistedOn(r.delist_date)
            : TEXT.cell.active,
    },
    { title: TEXT.col.first, dataIndex: "first_date", render: (v: string | null) => v ?? noBars },
    { title: TEXT.col.last, dataIndex: "last_date", render: (v: string | null) => v ?? noBars },
    { title: TEXT.col.bars, dataIndex: "bar_count", align: "right" },
    {
      title: TEXT.col.expected,
      dataIndex: "expected_days",
      align: "right",
      render: (v: number | null) => v ?? unknownTag,
    },
    {
      title: <span title={TEXT.cell.missingTip}>{TEXT.col.missing}</span>,
      dataIndex: "missing_days",
      align: "right",
      render: (v: number | null) => v ?? unknownTag,
    },
    {
      title: <span title={TEXT.cell.offCalendarTip}>{TEXT.col.offCalendar}</span>,
      dataIndex: "off_calendar_bars",
      align: "right",
      render: (v: number | null) => v ?? unknownTag,
    },
  ];

  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Space wrap>
        <Select
          allowClear
          placeholder={TEXT.filter.kind}
          style={{ width: 140 }}
          options={kinds.map((k) => ({ value: k, label: k }))}
          value={kind}
          onChange={(v: string | undefined) => {
            setKind(v);
            setPage(1);
          }}
        />
        <Typography.Text type="secondary">{TEXT.filter.note}</Typography.Text>
      </Space>
      {error !== undefined && (
        <Alert
          type="error"
          showIcon
          message={TEXT.tableError.title}
          description={TEXT.tableError.description(error.message)}
          action={<Button onClick={reload}>{TEXT.tableError.retry}</Button>}
        />
      )}
      {/* 读取失败时不显示上一次的表格，避免把旧数据当成现在的 */}
      {error === undefined && (
        <Table<CoverageRow>
          size="small"
          rowKey="symbol"
          columns={columns}
          dataSource={data?.items ?? []}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total: data?.total ?? 0,
            showSizeChanger: true,
            pageSizeOptions: PAGE_SIZES,
            onChange: (p, size) => {
              setPage(size === pageSize ? p : 1);
              setPageSize(size);
            },
          }}
          scroll={{ x: "max-content" }}
        />
      )}
    </Space>
  );
}

function Content({ data }: { data: Loaded }) {
  const { summary } = data;
  const groups = summary.groups;
  // 汇总是整个快照的全量，来自 /coverage/summary，不受表格筛选影响
  const byExchange = regroup(groups, (g) => g.exchange);
  const byKind = regroup(groups, (g) => g.kind);
  const byStatus = regroup(groups, (g) =>
    g.status === null ? null : g.status === "delisted" ? TEXT.summary.statusDelisted : TEXT.summary.statusActive,
  );
  const kinds = byKind.flatMap((g) => (g.label === null ? [] : [g.label]));

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <SnapshotInfo data={data} />
      {summary.total === 0 ? (
        <Alert type="info" showIcon message={TEXT.empty.title} />
      ) : (
        <>
          <div>
            <Typography.Title level={5}>{TEXT.summary.title}</Typography.Title>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <SummaryTable title={TEXT.summary.exchange} groups={byExchange} />
              <SummaryTable title={TEXT.summary.kind} groups={byKind} />
              <SummaryTable title={TEXT.summary.status} groups={byStatus} />
            </div>
            <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
              {TEXT.summary.statusNote(summary.as_of)}
            </Typography.Paragraph>
          </div>
          <CoverageTable snapshot={summary.snapshot} asOf={summary.as_of} kinds={kinds} />
        </>
      )}
    </Space>
  );
}

export default function CoveragePage() {
  const load = useCallback(() => loadCoverage(), []);
  const { data, error, loading, reload } = useRequest(load);
  // 每次重新读取都让表格重新挂载，回到第一页并重新读
  const [round, setRound] = useState(0);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space align="baseline">
        <Typography.Title level={3} style={{ margin: 0 }}>
          {TEXT.title}
        </Typography.Title>
        <Button
          onClick={() => {
            setRound((r) => r + 1);
            reload();
          }}
          loading={loading}
        >
          {TEXT.reload}
        </Button>
      </Space>
      <Typography.Text type="secondary">{TEXT.timeNote}</Typography.Text>
      <Alert type="info" showIcon message={TEXT.missingNote} />
      {loading && data === undefined && (
        <Space>
          <Spin />
          <Typography.Text type="secondary">{TEXT.loading}</Typography.Text>
        </Space>
      )}
      {error !== undefined && errorAlert(error, TEXT.unknown.title, TEXT.notFound.title)}
      {/* 重新读取失败时不显示上一次的结果，避免把旧数据当成现在的 */}
      {error === undefined && data !== undefined && <Content key={round} data={data} />}
    </Space>
  );
}
