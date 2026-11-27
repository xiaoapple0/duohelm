import { Alert, Button, Card, Checkbox, Descriptions, Input, Select, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useMemo, useState } from "react";

import { ApiError } from "../../api/client";
import { useRequest } from "../../api/useRequest";
import { formatShanghai, loadCoverage, summarize } from "./load";
import type { Group, Loaded, Row, Status } from "./load";
import { TEXT } from "./text";

const UNKNOWN_VALUE = "__unknown__";

function label(value: string | null): string {
  return value ?? TEXT.summary.unknownLabel;
}

function statusLabel(s: Status): string {
  return s === "active"
    ? TEXT.summary.statusActive
    : s === "delisted"
      ? TEXT.summary.statusDelisted
      : TEXT.summary.unknownLabel;
}

/** null 排在最后（升序时）。 */
function compareNullable<T extends string | number>(a: T | null, b: T | null): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return a < b ? -1 : a > b ? 1 : 0;
}

function SummaryTable({ title, groups }: { title: string; groups: Group[] }) {
  const total = groups.reduce((n, g) => n + g.count, 0);
  const data = [
    ...groups.map((g) => ({ key: String(g.label), ...g })),
    {
      key: "__total__",
      label: TEXT.summary.total,
      count: total,
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
          {data.snapshot}
        </Typography.Text>
      </Descriptions.Item>
      <Descriptions.Item label={TEXT.snapshot.createdAt}>{created}</Descriptions.Item>
      <Descriptions.Item label={TEXT.snapshot.state}>{state}</Descriptions.Item>
    </Descriptions>
  );
}

function Content({ data }: { data: Loaded }) {
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [exchange, setExchange] = useState<string>();
  const [kind, setKind] = useState<string>();
  const [status, setStatus] = useState<Status>();
  const [onlyMissing, setOnlyMissing] = useState(false);
  const [onlyNoBars, setOnlyNoBars] = useState(false);

  const rows = data.rows;
  // 汇总基于全部行，不受筛选影响
  const byExchange = useMemo(() => summarize(rows, (r) => r.exchange), [rows]);
  const byKind = useMemo(() => summarize(rows, (r) => r.kind), [rows]);
  const byStatus = useMemo(
    () => summarize(rows, (r) => (r.status === "unknown" ? null : statusLabel(r.status))),
    [rows],
  );

  const optionsOf = (groups: Group[]) =>
    groups.map((g) => ({
      value: g.label === null ? UNKNOWN_VALUE : g.label,
      label: label(g.label),
    }));

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const match = (value: string | null, want: string | undefined) =>
      want === undefined || (want === UNKNOWN_VALUE ? value === null : value === want);
    return rows.filter((r) => {
      if (q && !r.symbol.toLowerCase().includes(q) && !(r.name ?? "").toLowerCase().includes(q)) {
        return false;
      }
      if (!match(r.exchange, exchange) || !match(r.kind, kind)) return false;
      if (status !== undefined && r.status !== status) return false;
      if (onlyMissing && !(r.missing_days !== null && r.missing_days > 0)) return false;
      if (onlyNoBars && r.bar_count !== 0) return false;
      return true;
    });
  }, [rows, search, exchange, kind, status, onlyMissing, onlyNoBars]);

  const unknownTag = <Tag>{TEXT.cell.unknown}</Tag>;
  const columns: ColumnsType<Row> = [
    {
      title: TEXT.col.symbol,
      dataIndex: "symbol",
      sorter: (a, b) => a.symbol.localeCompare(b.symbol),
      defaultSortOrder: "ascend",
    },
    {
      title: TEXT.col.name,
      dataIndex: "name",
      sorter: (a, b) => compareNullable(a.name, b.name),
      render: (v: string | null) => v ?? <Typography.Text type="secondary">{TEXT.cell.noName}</Typography.Text>,
    },
    {
      title: TEXT.col.exchange,
      dataIndex: "exchange",
      sorter: (a, b) => compareNullable(a.exchange, b.exchange),
      render: (v: string | null) => v ?? unknownTag,
    },
    {
      title: TEXT.col.kind,
      dataIndex: "kind",
      sorter: (a, b) => compareNullable(a.kind, b.kind),
      render: (v: string | null) => v ?? unknownTag,
    },
    {
      title: TEXT.col.status,
      dataIndex: "status",
      sorter: (a, b) => a.status.localeCompare(b.status),
      render: (_: Status, r) =>
        r.status === "unknown"
          ? unknownTag
          : r.status === "delisted"
            ? TEXT.cell.delistedOn(r.delist_date ?? "")
            : TEXT.cell.active,
    },
    {
      title: TEXT.col.first,
      dataIndex: "first_date",
      sorter: (a, b) => compareNullable(a.first_date, b.first_date),
      render: (v: string | null) => v ?? <Typography.Text type="secondary">{TEXT.cell.noBars}</Typography.Text>,
    },
    {
      title: TEXT.col.last,
      dataIndex: "last_date",
      sorter: (a, b) => compareNullable(a.last_date, b.last_date),
      render: (v: string | null) => v ?? <Typography.Text type="secondary">{TEXT.cell.noBars}</Typography.Text>,
    },
    {
      title: TEXT.col.bars,
      dataIndex: "bar_count",
      align: "right",
      sorter: (a, b) => a.bar_count - b.bar_count,
    },
    {
      title: TEXT.col.expected,
      dataIndex: "expected_days",
      align: "right",
      sorter: (a, b) => compareNullable(a.expected_days, b.expected_days),
      render: (v: number | null) => v ?? unknownTag,
    },
    {
      title: <span title={TEXT.cell.missingTip}>{TEXT.col.missing}</span>,
      dataIndex: "missing_days",
      align: "right",
      sorter: (a, b) => compareNullable(a.missing_days, b.missing_days),
      render: (v: number | null) => v ?? unknownTag,
    },
    {
      title: <span title={TEXT.cell.offCalendarTip}>{TEXT.col.offCalendar}</span>,
      dataIndex: "off_calendar_bars",
      align: "right",
      sorter: (a, b) => compareNullable(a.off_calendar_bars, b.off_calendar_bars),
      render: (v: number | null) => v ?? unknownTag,
    },
  ];

  const filtering =
    search !== "" ||
    exchange !== undefined ||
    kind !== undefined ||
    status !== undefined ||
    onlyMissing ||
    onlyNoBars;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <SnapshotInfo data={data} />
      {data.rows.length !== data.total && (
        <Alert
          type="warning"
          showIcon
          message={TEXT.partial.title(data.rows.length, data.total)}
          description={TEXT.partial.description}
        />
      )}
      {data.securitiesProblem !== undefined && (
        <Alert
          type="warning"
          showIcon
          message={TEXT.securitiesUnknown.title}
          description={TEXT.securitiesUnknown.description(data.securitiesProblem)}
        />
      )}
      {rows.length === 0 ? (
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
              {TEXT.summary.statusNote}
            </Typography.Paragraph>
          </div>
          <Space wrap>
            <Input.Search
              allowClear
              placeholder={TEXT.filter.search}
              style={{ width: 200 }}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                if (e.target.value === "") setSearch("");
              }}
              onSearch={setSearch}
            />
            <Select
              allowClear
              placeholder={TEXT.filter.exchange}
              style={{ width: 120 }}
              options={optionsOf(byExchange)}
              value={exchange}
              onChange={setExchange}
            />
            <Select
              allowClear
              placeholder={TEXT.filter.kind}
              style={{ width: 120 }}
              options={optionsOf(byKind)}
              value={kind}
              onChange={setKind}
            />
            <Select
              allowClear
              placeholder={TEXT.filter.status}
              style={{ width: 120 }}
              options={[
                { value: "active", label: TEXT.summary.statusActive },
                { value: "delisted", label: TEXT.summary.statusDelisted },
                { value: "unknown", label: TEXT.summary.unknownLabel },
              ]}
              value={status}
              onChange={setStatus}
            />
            <Checkbox checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)}>
              {TEXT.filter.onlyMissing}
            </Checkbox>
            <Checkbox checked={onlyNoBars} onChange={(e) => setOnlyNoBars(e.target.checked)}>
              {TEXT.filter.onlyNoBars}
            </Checkbox>
            {filtering && (
              <Button
                onClick={() => {
                  setSearch("");
                  setDraft("");
                  setExchange(undefined);
                  setKind(undefined);
                  setStatus(undefined);
                  setOnlyMissing(false);
                  setOnlyNoBars(false);
                }}
              >
                {TEXT.filter.reset}
              </Button>
            )}
            <Typography.Text type="secondary">
              {TEXT.filter.shown(filtered.length, rows.length)}
            </Typography.Text>
          </Space>
          <Table<Row>
            size="small"
            rowKey="symbol"
            columns={columns}
            dataSource={filtered}
            pagination={{ defaultPageSize: 50, showSizeChanger: true, pageSizeOptions: [50, 100, 200] }}
            scroll={{ x: "max-content" }}
          />
        </>
      )}
    </Space>
  );
}

export default function CoveragePage() {
  const load = useCallback(() => loadCoverage(), []);
  const { data, error, loading, reload } = useRequest(load);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space align="baseline">
        <Typography.Title level={3} style={{ margin: 0 }}>
          {TEXT.title}
        </Typography.Title>
        <Button onClick={reload} loading={loading}>
          {TEXT.reload}
        </Button>
      </Space>
      <Typography.Text type="secondary">{TEXT.timeNote}</Typography.Text>
      <Alert type="info" showIcon message={TEXT.missingNote} />
      {loading && data === undefined && <Spin tip={TEXT.loading} />}
      {error !== undefined &&
        (error instanceof ApiError && error.status === 404 ? (
          <Alert
            type="warning"
            showIcon
            message={TEXT.notFound.title}
            description={TEXT.notFound.description(error.message)}
          />
        ) : (
          <Alert
            type="error"
            showIcon
            message={TEXT.unknown.title}
            description={TEXT.unknown.description(error.message)}
          />
        ))}
      {/* 重新读取失败时不显示上一次的结果，避免把旧数据当成现在的 */}
      {error === undefined && data !== undefined && <Content data={data} />}
    </Space>
  );
}
