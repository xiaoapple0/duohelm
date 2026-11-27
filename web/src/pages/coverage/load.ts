// 覆盖情况页面的取数和汇总整理。纯数据逻辑，不含界面。
import { dataApi } from "../../api/data";
import type { CoverageGroup, CoverageSummaryOut, Snapshot } from "../../api/data";

export const PAGE_SIZES = [50, 100, 200];

export type SnapshotInfo =
  | { kind: "ok"; value: Snapshot }
  /** 快照列表读到了，但里面没有这个快照（可能已被清理） */
  | { kind: "notInList" }
  | { kind: "error"; message: string };

export interface Loaded {
  summary: CoverageSummaryOut;
  /** 创建时间、是否当前快照：快照列表读不到、或不在列表里时未知 */
  info: SnapshotInfo;
}

/**
 * 取汇总和快照信息。汇总不指定快照，拿到它报告的快照 id 之后，
 * 表格的各页都固定读这个快照，不会混合两个快照。
 * 汇总失败就抛错（整体未知）；快照列表失败只让创建时间等字段未知。
 */
export async function loadCoverage(): Promise<Loaded> {
  const summary = await dataApi.coverageSummary();
  let info: SnapshotInfo;
  try {
    const snapshots = await dataApi.snapshots();
    const hit = snapshots.snapshots.find((s) => s.id === summary.snapshot);
    info = hit ? { kind: "ok", value: hit } : { kind: "notInList" };
  } catch (err) {
    info = { kind: "error", message: err instanceof Error ? err.message : String(err) };
  }
  return { summary, info };
}

export interface Group {
  label: string | null; // null：未知
  count: number;
  noBars: number;
  withMissing: number;
}

/** 把接口的分组按某个维度合并。key 为 null 的归到“未知”。 */
export function regroup(groups: CoverageGroup[], key: (g: CoverageGroup) => string | null): Group[] {
  const map = new Map<string | null, Group>();
  for (const g of groups) {
    const label = key(g);
    let out = map.get(label);
    if (!out) {
      out = { label, count: 0, noBars: 0, withMissing: 0 };
      map.set(label, out);
    }
    out.count += g.securities;
    out.noBars += g.no_bars;
    out.withMissing += g.with_missing;
  }
  return [...map.values()].sort((a, b) => {
    if (a.label === null) return 1;
    if (b.label === null) return -1;
    return a.label.localeCompare(b.label);
  });
}

/** 北京时间的时间显示（不依赖本机时区）。 */
export function formatShanghai(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const pick = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${pick("year")}-${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}:${pick("second")}`;
}
