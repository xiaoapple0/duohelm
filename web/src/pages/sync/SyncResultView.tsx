import { Descriptions, Typography } from "antd";
import type { ReactNode } from "react";

import type { DataSyncResult, JsonObject } from "../../api/client";
import { formatJson } from "../tasks/format";
import { TEXT } from "./text";

const T = TEXT.result;

/** 下面单独显示的字段；结果里其他的字段原样列在“其他字段”里。 */
const KNOWN = new Set([
  "plugin_id",
  "start",
  "end",
  "snapshot_id",
  "connection",
  "securities",
  "symbols",
  "written",
  "dropped_zero_volume",
  "scanned",
  "rescanned",
  "rechecked",
  "snapshot_date",
  "snapshot_skipped",
  "st_snapshot_failed",
  "st_unknown_names",
  "index_snapshot_failed",
]);

const preStyle = { margin: 0, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-all" } as const;

function show(value: unknown): ReactNode {
  if (value === null || value === undefined) {
    return "-";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return <pre style={preStyle}>{formatJson(value)}</pre>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function Written({ value }: { value: unknown }) {
  if (!isRecord(value)) {
    return <>{show(value)}</>;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) {
    return <Typography.Text type="secondary">{T.writtenNone}</Typography.Text>;
  }
  return (
    <div>
      {entries.map(([dataset, rows]) => (
        <div key={dataset}>
          <Typography.Text code>{dataset}</Typography.Text> {show(rows)}
        </div>
      ))}
    </div>
  );
}

export default function SyncResultView({ result }: { result: JsonObject }) {
  const r = result as Partial<DataSyncResult> & JsonObject;
  const rows: { key: string; label: string; hint?: string; value: ReactNode }[] = [];
  const add = (key: string, label: string, value: ReactNode, hint?: string) =>
    rows.push({ key, label, value, hint });

  add(
    "snapshot_id",
    T.snapshotId,
    r.snapshot_id ? (
      <Typography.Text code copyable>
        {r.snapshot_id}
      </Typography.Text>
    ) : (
      <Typography.Text type="secondary">{T.noNewSnapshot}</Typography.Text>
    ),
  );
  add("written", T.written, <Written value={r.written} />);
  add("dropped_zero_volume", T.dropped, show(r.dropped_zero_volume), T.droppedHint);
  add("plugin_id", T.plugin, show(r.plugin_id));
  if (r.start !== undefined || r.end !== undefined) {
    add("range", T.range, `${String(r.start ?? "-")} ~ ${String(r.end ?? "-")}`);
  }
  add("securities", T.securities, show(r.securities));
  add("symbols", T.symbols, show(r.symbols));
  if (r.connection) {
    add("connection", T.connection, show(r.connection));
  }
  add("scanned", T.scanned, show(r.scanned));
  add("rescanned", T.rescanned, show(r.rescanned));
  add("rechecked", T.rechecked, show(r.rechecked));
  add("snapshot_date", T.snapshotDate, show(r.snapshot_date));
  if (r.snapshot_skipped) {
    add("snapshot_skipped", T.snapshotSkipped, show(r.snapshot_skipped));
  }
  if (r.st_snapshot_failed) {
    add("st_snapshot_failed", T.stFailed, show(r.st_snapshot_failed));
  }
  add("st_unknown_names", T.stUnknown, show(r.st_unknown_names));
  if (isRecord(r.index_snapshot_failed) && Object.keys(r.index_snapshot_failed).length > 0) {
    add("index_snapshot_failed", T.indexFailed, show(r.index_snapshot_failed));
  }

  const extras = Object.fromEntries(Object.entries(result).filter(([k]) => !KNOWN.has(k)));
  if (Object.keys(extras).length > 0) {
    add("others", T.others, show(extras));
  }

  return (
    <Descriptions column={1} size="small" bordered>
      {rows.map((row) => (
        <Descriptions.Item
          key={row.key}
          label={
            row.hint ? (
              <span title={row.hint}>{row.label}</span>
            ) : (
              row.label
            )
          }
        >
          {row.value}
        </Descriptions.Item>
      ))}
    </Descriptions>
  );
}
